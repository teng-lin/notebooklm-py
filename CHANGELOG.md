# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.7.0] - 2026-06-03

### Highlights

- **v0.8.0 error-contract runway.** This release lands the *additive half* of a
  cross-SDK convergence on "absence and refusal **raise**; only success and
  async-lifecycle state are returned." You can adopt the forward-compatible form
  today and run on both 0.7.0 and 0.8.0 with no flag day:
  - **Test your code against 0.8.0 today** — set `NOTEBOOKLM_FUTURE_ERRORS=1` to
    opt your process into the v0.8.0 error contract (`get()` raises
    `*NotFoundError` on a miss, all dict-style access on the typed returns
    (`[...]`, `.get()`, `in`, `.keys()`, …) raises, and
    the deprecated `wait_for_completion(interval=...)` alias raises) **without
    changing default behavior**. Run your test suite with it on to find breakage
    before you upgrade. This is the "test-before-you-migrate" mechanism paired
    with the [Upgrading to v0.8.0](docs/upgrading-to-0.8.0.md) guide.
  - **`get_or_none()`** — a new, **silent** optional lookup on
    `sources` / `artifacts` / `notes` / `mind_maps` that returns the object or
    `None` and never warns. It is the sanctioned replacement for the now-soft
    `get()`-returns-`None` pattern.
  - **`get()` now warns on a miss** (still returns `None` this release) and will
    **raise** the typed `*NotFoundError` for its domain in v0.8.0 (#1247).
  - **Typed `*NotFoundError` per domain** — `NoteNotFoundError` /
    `MindMapNotFoundError` join the existing source / artifact / notebook errors,
    all catchable via the `NotFoundError` umbrella.
- **Breaking: `rename()` returns the renamed object; `delete()` returns `None`.**
  `rename()` now re-fetches and returns the live object (raising `*NotFoundError`
  on a missing target), and `delete()` returns `None` and is idempotent on an
  already-absent target. See **Breaking changes** below before upgrading.
- **Typed dataclass returns** for `research.poll` / `start` /
  `wait_for_completion`, `artifacts.generate_mind_map`, and `sources.get_guide`
  (`ResearchStatus`, `ResearchTask`, `ResearchSource`, `ResearchStart`,
  `MindMapResult`, `SourceGuide`) — attribute access instead of untyped dicts,
  with a backward-compatible read-only mapping bridge.
- **Unified `client.mind_maps` surface** over both backends (note-backed +
  interactive), plus **`client.artifacts.retry_failed()`** to retry a failed
  Studio artifact in place (and a matching `notebooklm artifact retry` command).

### Breaking changes

> **⚠ BREAKING — `rename()` returns the renamed object; `delete()` returns `None`.**
>
> These return-type changes ship **now, as a clean break with no deprecation
> runway**, because the old returns were never usable contracts a caller could
> depend on in good faith. (Contrast `get()`'s `None`-on-miss, which **is** a
> real, documented contract and keeps its full deprecation runway to v0.8.0 —
> see issue #1247. The coherent story: **reads/renames are missing-strict;
> deletes are absence-idempotent.**)
>
> **`rename()` → returns the renamed object, raises `*NotFoundError` on a
> missing target** (issues #1255, #1256):
>
> - `artifacts.rename` previously returned `None` **even on success** (an
>   unusable return); it now re-fetches and returns the renamed `Artifact`,
>   raising `ArtifactNotFoundError` when the target is absent.
> - `sources.rename` previously **fabricated** an unverified
>   `Source(id, title)` when the RPC echoed nothing (a silent-false-success
>   bug); it now prefers the `UPDATE_SOURCE` echo, falls back to an internal
>   fetch, returns the real `Source`, and raises `SourceNotFoundError` when the
>   target is absent. **The fabrication is gone.**
> - `notebooks.rename` already returned the re-fetched `Notebook` (the
>   reference behavior) — unchanged.
> - `mind_maps.rename` (both note-backed and interactive backings) now returns
>   the renamed `MindMap` and raises on a missing target.
> - **Error taxonomy:** only *genuine absence* (empty-payload / absent-from-list,
>   detected via a content/list lookup — **not** a transport 404) maps to a
>   `*NotFoundError`. Transport / `429` / `5xx` / auth errors propagate **as
>   themselves** and are never laundered into a synthetic `*NotFoundError`.
> - **Bulk opt-out:** every `rename()` accepts `return_object: bool = True`.
>   Pass `return_object=False` to skip the hydrate re-fetch and return `None`
>   (artifacts' re-fetch is a full `LIST_ARTIFACTS`, so bulk renamers that
>   ignore the return should opt out to avoid N extra list calls).
>
> **`delete()` → returns `None`, idempotent on a missing target** (issue #1211):
>
> - `notebooks` / `sources` / `artifacts` / `notes.delete` and
>   `notes.delete_mind_map` (and `mind_maps.delete`) previously returned a
>   hardcoded `True`; they now return `None`. The old `True` was a tautology
>   (never `False`), but `True → None` is a *real, observable* flip from truthy
>   to falsy:
>   ```python
>   # BEFORE (entered the block; delete always returned True)
>   if await client.sources.delete(nb_id, src_id):
>       ...  # this branch no longer runs — delete() now returns None (falsy)
>   ```
>   Drop the `if`; call `delete()` for its effect. Use `get()` first if you
>   need to assert existence.
> - **Idempotent:** deleting an already-absent target **succeeds** (returns
>   `None`); it does **not** raise `*NotFoundError`. This matches HTTP `DELETE`
>   idempotency and keeps retry/teardown loops clean. (The one exception is
>   `mind_maps.delete` *without* an explicit `kind`, which must list to pick
>   the right RPC family and so raises `ValueError` for an unknown id; pass
>   `kind=` to delete idempotently.)
> - **Real failures still raise:** `allow_null=True` tolerates only a null
>   *result*, not an RPC/HTTP error — a `403` / `5xx` / auth / transport
>   failure on delete still propagates. "Idempotent on missing" is not "swallow
>   all errors."

> **⚠ BREAKING — lapsed v0.6.0-targeted deprecations removed.**
>
> These deprecation shims advertised removal in v0.6.0, which has shipped, so
> they have now been removed. This is a pre-1.0 breaking change. See
> [`docs/deprecations.md`](docs/deprecations.md) "Removed in v0.7.0".
>
> - **Positional `wait` / `wait_timeout` on `SourcesAPI.add_url` / `add_text` /
>   `add_file` / `add_drive`** — these parameters are now **keyword-only**.
>   Passing them positionally raises `TypeError`.
>   ```python
>   # BEFORE (deprecated, emitted DeprecationWarning)
>   await client.sources.add_url(nb_id, url, True, 45.0)
>   # AFTER
>   await client.sources.add_url(nb_id, url, wait=True, wait_timeout=45.0)
>   ```
> - **`ArtifactsAPI.wait_for_completion(poll_interval=...)`** — the deprecated
>   `poll_interval` alias was removed; use `initial_interval=...` (same
>   cadence). Passing `poll_interval` raises `TypeError`.
>   ```python
>   # BEFORE
>   await client.artifacts.wait_for_completion(nb_id, task_id, poll_interval=5.0)
>   # AFTER
>   await client.artifacts.wait_for_completion(nb_id, task_id, initial_interval=5.0)
>   ```
> - **`NOTEBOOKLM_STRICT_DECODE=0` soft-mode opt-out** — removed. Strict
>   decoding is now the only mode: schema-drift helpers (notably `safe_index`)
>   always raise `UnknownRPCMethodError` on shape drift instead of
>   warn-and-returning `None` / `[]`. The env var is now ignored (no-op).
>   Callers that previously relied on the soft fallback should handle
>   `UnknownRPCMethodError` (a subclass of `RPCError` / `DecodingError`).
> - **`NotesAPI.create_from_chat(...)`** — removed (deprecated since v0.5.0,
>   two MINOR cycles of warnings served; the documented removal target was
>   v0.7.0). It was a pure forwarder. Use `ChatAPI.save_answer_as_note(...)`,
>   the canonical citation-rich saved-from-chat method and data owner
>   (ADR-013): `await client.chat.save_answer_as_note(nb_id, ask_result)`.
>   The now-unused `save_chat_answer` injection plumbing on `NotesAPI` was
>   removed with it.
>
> **Not removed:** `SourcesAPI.add_file(mime_type=...)` and
> `notebooklm source add --mime-type` (file sources) were **reassessed and
> kept** — `mime_type` was re-wired to set the resumable-upload content-type
> header (overriding filename-extension inference), so it is a supported
> parameter, not a dead shim. Its stale `DeprecationWarning` had already been
> removed; the documentation now reflects this.
>
> **Not removed:** awaiting `NotebookLMClient.from_storage(...)` still works —
> its deprecation targets v1.0, not v0.6.0.

### Added

- `get_or_none()` — the sanctioned **silent** optional lookup, added to
  `client.sources` / `client.artifacts` / `client.notes` / `client.mind_maps`.
  It returns the entity (`Source` / `Artifact` / `Note` / `MindMap`) or `None`
  for a genuine absence and **never warns**, making it the drop-in migration
  target for the now-deprecated `get()`-returns-`None` pattern (see
  **Deprecated** below; issue #1247). Unlike `get()`, it does **not** swallow
  transport, auth, or decode faults — only a real "not found" yields `None`.
  ```python
  # Silent optional lookup (no DeprecationWarning):
  src = await client.sources.get_or_none(nb_id, source_id)
  if src is None:
      ...
  ```
  Additive (ADR-019; issue #1247).
- `NOTEBOOKLM_FUTURE_ERRORS` opt-in preview flag — run the **v0.8.0 error
  contract** early to test forward-compatibility before the breaking flips ship
  (ADR-0019 / umbrella #1346). Default-off and **byte-identical** to current
  v0.7.0 behavior; when truthy (`1`/`true`/`yes`/`on`) the three warn-runways
  adopt their v0.8.0 raise-target: `sources.get` / `artifacts.get` /
  `notes.get` / `mind_maps.get` raise the matching `*NotFoundError` on a miss
  (#1247), the **whole** `MappingCompatMixin` mapping surface — `[...]`
  subscript plus the silent `get` / `keys` / `items` / `values` / `len` / `in` /
  `iter` shims — raises the exact error a bare dataclass would (#1251), and
  the deprecated `ResearchAPI.wait_for_completion(interval=...)` alias raises
  `TypeError` (#1254). Takes precedence over `NOTEBOOKLM_QUIET_DEPRECATIONS`
  (a runway raises regardless of quiet). The four `get()` methods are now routed
  through a single `_lookup.resolve_get` bridge, eliminating the hand-duplicated
  warn-on-miss pattern. Helper: `notebooklm._deprecation.future_errors_enabled`.
  The flag now **also** previews the purely-behavioral v0.8.0 changes that have
  no warn-runway (#1405): the uninformative `bool` returns of `sources.refresh`
  and `chat.delete_conversation` become `None` (#1290); a synchronous generation
  refusal **raises** the decoder's `RateLimitError` / `RPCError` /
  `DecodingError` / `ArtifactFeatureUnavailableError` instead of being swallowed
  into `GenerationStatus(status="failed")` / returned `None` — across
  `_call_generate`, `revise_slide`, `_parse_generation_result`, and
  `research.start` (#1342); and the mutate-existing ops `notes.update` and
  `sources`/`artifacts` `rename(return_object=False)` fail loud with a
  `*NotFoundError` on a missing target (#1362). These previews are runtime-only —
  **no public return annotation changes** until the v0.8.0 flip — so default-off
  stays byte-identical. Does **not** close #1247/#1251/#1254/#1290/#1342/#1362 —
  the runways and current behavior remain until the v0.8.0 flip. See
  `docs/deprecations.md`. Additive (issues #1346, #1405).
- `client.artifacts.retry_failed(notebook_id, artifact_id)` — retry a failed
  Studio artifact in place (the web UI "Retry" action), via the new
  `RETRY_ARTIFACT` (`Rytqqe`) RPC. The artifact is not deleted first and the
  same `artifact_id` is preserved, so existing `poll_status()` /
  `wait_for_completion()` flows keep working. Follows the ADR-019 "async
  kickoff" contract: an accepted retry returns
  `GenerationStatus(status="in_progress")`, while a synchronous refusal
  (`USER_DISPLAYABLE_ERROR` — rate limit / quota / not-retryable) **raises** the
  underlying `RateLimitError` / `RPCError` rather than returning a
  `status="failed"` handle. New `notebooklm artifact retry <artifact_id>
  [--wait] [--json]` CLI command. Additive (issues #1319, #1346).
- `notebooklm.artifacts.with_rate_limit_retry` now also retries when the
  wrapped callable **raises** `RateLimitError` (backing off and re-raising once
  the retry budget is exhausted), so it can wrap the new `retry_failed`. The
  existing returned-rate-limited-`GenerationStatus` path (used by `generate_*`)
  is unchanged — this is a backward-compatible addition (issue #1319).
- New public exception types for the note and mind-map domains, mirroring the
  existing `SourceError` / `SourceNotFoundError` shape: `NoteError` +
  `NoteNotFoundError` and `MindMapError` + `MindMapNotFoundError`. Each
  `*NotFoundError` is a triple-base `(NotFoundError, RPCError, <Domain>Error)`,
  so it is catchable via the cross-domain `NotFoundError` umbrella, at
  transport-level `except RPCError` call sites, and at domain-level
  `except NoteError` / `except MindMapError` call sites. These are the
  prerequisite for the mind-map not-found work (ADR-019; issues #1291, #1346).
  `MindMapNotFoundError` is now raised by the `mind_maps` mutation paths (see
  *Changed* below); `NoteNotFoundError` is not raised by any method yet.
- `ResearchStatus.NOT_FOUND` — a typed lifecycle sentinel for the
  poll-observed absence of a *specific* requested research task, distinct from
  `NO_RESEARCH` ("nothing in flight"). `research.poll(notebook_id, task_id=...)`
  now returns `ResearchTask.not_found(task_id)` (status `NOT_FOUND`, carrying
  the requested id) when a non-empty pinned `task_id` matches no in-flight task;
  the unfiltered `task_id=None` empty poll still returns `NO_RESEARCH`
  unchanged. Additive and non-breaking — the poll never raises for an absent
  task (ADR-019 Rule 4; issues #1344, #1346).
- **Typed return values for the research / mind-map / source-guide methods.**
  `research.poll` / `research.start` / `research.wait_for_completion`,
  `artifacts.generate_mind_map`, and `sources.get_guide` now return typed
  dataclasses instead of untyped `dict[str, Any]`, with a new
  `ResearchStatus` str-enum for the status field. The new public types are
  exported from `notebooklm` and `notebooklm.types`:
  `ResearchStatus`, `ResearchTask`, `ResearchSource`, `ResearchStart`,
  `MindMapResult`, and `SourceGuide`.
  ```python
  from notebooklm import ResearchStatus

  result = await client.research.poll(nb_id)
  if result.status == ResearchStatus.COMPLETED:   # also == "completed"
      for source in result.sources:
          print(source.title, source.url)

  guide = await client.sources.get_guide(nb_id, src_id)
  print(guide.summary, guide.keywords)

  mind_map = await client.artifacts.generate_mind_map(nb_id)
  print(mind_map.note_id, mind_map.mind_map)
  ```
  This is **backward-compatible**: `ResearchStatus` is a `str` enum (so
  `status == "completed"` still holds), and the returned dataclasses keep
  working as read-only mappings — `result["status"]` / `result.get("status")`
  / `result.keys()` / `"status" in result` all still work (subscript emits a
  `DeprecationWarning`; see **Deprecated** below). The dict-subscript bridge is
  removed in v0.8.0.
- **`WaitTimeoutError` — one catchable base for every wait/poll timeout.** A
  new public exception (`notebooklm.WaitTimeoutError`) is the common base of
  `SourceTimeoutError`, `ArtifactTimeoutError` (and its
  `ArtifactPendingTimeoutError` / `ArtifactInProgressTimeoutError` subclasses),
  and the new `ResearchTimeoutError`, so a single `except WaitTimeoutError`
  clause catches a wait timeout from any domain. It mixes in the built-in
  `TimeoutError`, so this is **fully backward-compatible**: existing
  `except TimeoutError` clauses keep catching every wait timeout unchanged.
  ```python
  from notebooklm import WaitTimeoutError
  try:
      await client.sources.wait_until_ready(nb_id, src_id)
      await client.artifacts.wait_for_completion(nb_id, task_id)
      await client.research.wait_for_completion(nb_id, research_task_id)
  except WaitTimeoutError:   # was three separate / inconsistent timeout types
      ...
  ```
- **`ResearchError` / `ResearchTimeoutError`.** The research domain gained a
  catchable base (`ResearchError`, mirroring `SourceError` / `ArtifactError`)
  and a domain timeout (`ResearchTimeoutError`). `ResearchAPI.wait_for_completion`
  previously raised the bare built-in `TimeoutError`; it now raises
  `ResearchTimeoutError`, a `WaitTimeoutError` (and therefore still a
  `TimeoutError`), exposing `notebook_id` / `task_id` / `timeout` /
  `timeout_seconds` / `last_status`. (`ResearchTaskMismatchError` stays a
  `ValidationError` — it is caller-input validation, not a wait timeout.)

### Changed / Deprecated

- `ArtifactTimeoutError` now declares its bases umbrella-first
  (`WaitTimeoutError, ArtifactError`), matching `SourceTimeoutError` and
  `ResearchTimeoutError`. This is a cosmetic reorder with no behavior change:
  `isinstance`/`except` against either base is unaffected.
- `client.mind_maps` mutation sites now raise `MindMapNotFoundError` instead of
  a bare `ValueError` on a missing target, so callers can `except NotFoundError`
  (or `except MindMapError`) uniformly across namespaces. `rename` (and the
  underlying note-backed `rename_mind_map`) raise it; `MindMapNotFoundError`
  multi-inherits `ValueError`'s sibling `NotFoundError`, **not** `ValueError`
  itself, so existing `except ValueError` rename callers must switch to
  `except NotFoundError` / `except MindMapNotFoundError`. `delete(kind=None)` is
  now **idempotent** — deleting an already-absent mind map returns `None` rather
  than raising (matching `sources`/`artifacts`/`notes` delete, and the
  `kind`-supplied path). `get_tree` returns `None` for a missing mind map (it is
  a derived read that does not police parent existence) — previously `kind=None`
  raised on an unknown id. Shape-drift in the interactive payload still raises
  `UnknownRPCMethodError` (ADR-019; issues #1291, #1346).
- `client.mind_maps.generate(kind=MindMapKind.INTERACTIVE)` now raises
  `ArtifactFeatureUnavailableError` (instead of a bare `ArtifactError`) when the
  `CREATE_ARTIFACT` call returns no artifact id — no generation task was
  created. **Non-breaking for `except ArtifactError`**:
  `ArtifactFeatureUnavailableError` is a subclass of `ArtifactError`, so that
  catch still works. (It also multi-inherits `RPCError`, so a handler that does
  `except RPCError` *before* `except ArtifactError` will now take the `RPCError`
  branch — the same MRO the sibling `generate_*` / `retry_failed` null-create
  paths already produce.) This aligns the interactive async kickoff with that
  sibling null-create contract (ADR-019 "async kickoff"; issue #1359).
- Documented two pre-existing `client.mind_maps` read semantics (docs-only, no
  behavior change): `list()` populates `MindMap.tree` only for note-backed
  entries — interactive entries carry `tree=None` ("not fetched", not "empty";
  call `get_tree(..., kind=MindMapKind.INTERACTIVE)` to fetch one); and the explicit
  `get_tree(..., kind=MindMapKind.INTERACTIVE)` path delegates absence detection to the RPC,
  so a missing id's value is server-dependent (returns `None` today) rather than
  enforced client-side (issues #1355, #1359).
- **`ResearchAPI.wait_for_completion(interval=...)` → `initial_interval=...`.**
  The research waiter's poll-cadence keyword is now `initial_interval`,
  matching `SourcesAPI.wait_until_ready` and
  `ArtifactsAPI.wait_for_completion`. The old `interval=` keyword still works
  as a **deprecated alias** (warns in 0.7.0, removed in v0.8.0): passing a
  non-default value emits a `DeprecationWarning` (suppressible with
  `NOTEBOOKLM_QUIET_DEPRECATIONS=1`), and passing both `interval` and
  `initial_interval` raises `TypeError`. Default-shape calls stay silent and
  the signature is otherwise unchanged, so the public-API compatibility audit
  stays clean. See [`docs/deprecations.md`](docs/deprecations.md) for the
  migration.
  > **Decision — `wait_timeout` kept.** The `wait_timeout` keyword on the
  > `SourcesAPI.add_*` family was deliberately **not** renamed to `timeout`:
  > on those add methods `timeout` would be ambiguous with a per-request HTTP
  > timeout, whereas the dedicated waiter methods already spell their budget
  > `timeout`. The research `interval` → `initial_interval` rename was the only
  > standardization with a clear, unambiguous win.

### Deprecated

> Every deprecation below is on a compatibility runway to **v0.8.0**. The
> consolidated [Upgrading to v0.8.0](docs/upgrading-to-0.8.0.md) guide is the
> single reference for moving your code across the boundary; set
> `NOTEBOOKLM_FUTURE_ERRORS=1` to exercise the v0.8.0 behavior in your tests
> today.

- **`client.mind_maps.get()` returning `None` for a missing mind map is now
  deprecated**, closing the runway gap that left `mind_maps` as the only
  #1247-cohort namespace without one. It now emits a `DeprecationWarning` on a
  miss while **still returning `None`** (behavior unchanged this release),
  matching `sources.get()` / `artifacts.get()` / `notes.get()`. In **v0.8.0** it
  will instead **raise** `MindMapNotFoundError`. Use `get_or_none()` for the
  sanctioned optional lookup (it stays silent), or migrate the `None`-check to a
  `try/except MindMapNotFoundError`. The warning fires only on a miss; suppress
  it with `NOTEBOOKLM_QUIET_DEPRECATIONS=1`. Tracking issue: #1247 (gap: #1358).
  See [`docs/deprecations.md`](docs/deprecations.md).
- **`sources.get()` / `artifacts.get()` / `notes.get()` returning `None` for a
  missing entity is deprecated.** These three methods now emit a
  `DeprecationWarning` on a miss while **still returning `None`** (behavior is
  unchanged this release). In **v0.8.0** they will instead **raise** the
  matching `*NotFoundError` (`SourceNotFoundError` / `ArtifactNotFoundError` /
  `NoteNotFoundError`), unifying the not-found contract with `notebooks.get()`,
  which already raises `NotebookNotFoundError`. Tracking issue: #1247.
  ```python
  # Migrate the None-check to a try/except before v0.8.0:
  # BEFORE (deprecated)
  src = await client.sources.get(nb_id, source_id)
  if src is None:
      ...
  # AFTER
  try:
      src = await client.sources.get(nb_id, source_id)
  except SourceNotFoundError:
      ...
  ```
  The warning fires only on a miss; successful lookups stay silent. Suppress it
  with `NOTEBOOKLM_QUIET_DEPRECATIONS=1`. See
  [`docs/deprecations.md`](docs/deprecations.md).
- **Dict-subscript access on the new typed research / mind-map / guide
  returns is deprecated.** Now that `research.poll` / `research.start` /
  `research.wait_for_completion`, `artifacts.generate_mind_map`, and
  `sources.get_guide` return typed dataclasses (see **Added**), the legacy
  `result["status"]` dict-subscript access emits a `DeprecationWarning` and
  will be **removed in v0.8.0**. Migrate to attribute access (`result.status`).
  The silent `result.get(...)` / `result.keys()` / `"x" in result` mapping
  shims also disappear in v0.8.0. Suppress the warning with
  `NOTEBOOKLM_QUIET_DEPRECATIONS=1`. See
  [`docs/deprecations.md`](docs/deprecations.md).
  ```python
  # BEFORE (still works in 0.7.0, warns on subscript)
  if result["status"] == "completed":
      sources = result["sources"]
  # AFTER
  if result.status == "completed":
      sources = result.sources
  ```

### Fixed

- **CLI now emits the `NOT_FOUND` error envelope for the `*NotFoundError`
  family from the centralized handler, instead of the generic
  `NOTEBOOKLM_ERROR`.** Any `NotebookNotFoundError` / `SourceNotFoundError` /
  `ArtifactNotFoundError` / `NoteNotFoundError` / `MindMapNotFoundError` that
  reaches `cli/error_handler.py` (e.g. `notebooks.get()` on a missing notebook,
  or a `rename` whose target was deleted mid-operation) now exits `1` with the
  typed `{"error": true, "code": "NOT_FOUND", ...}` JSON envelope carrying the
  missing resource id — matching the per-command `source` / `artifact` /
  `note get` convention (the documented CLI not-found contract since v0.5.0).
  The per-command `get` paths already used `get_or_none` and are unaffected.
  This also makes the `NOTEBOOKLM_FUTURE_ERRORS=1` preview faithful at the CLI
  boundary, pre-positioning it for the v0.8.0 `get()` → raise / mutate-existing
  fail-loud flips (issues #1364, #1247, #1362).
- **`Source.from_api_response` now reports the real processing `status`.** The
  `ADD_SOURCE` / rename parsing path previously never read the status block and
  always fell back to `SourceStatus.READY`, while `client.sources.list()` /
  `get()` and the source poller read the decoded status. Both parsers now
  funnel through a single `Source.from_row` construction site, so a `Source`
  produced from an add/rename response carries the same `status` (and `url` /
  `created_at`) as the listing path. The `Source.status` field annotation was
  also corrected from `int` to `SourceStatus` (still an `int`-compatible enum).

## [0.6.0] - 2026-05-29

### Breaking changes

> **⚠ BREAKING — exception hierarchy symmetry restored.**
>
> `SourceNotFoundError` and `ArtifactNotFoundError` now inherit from `RPCError`
> in addition to their respective domain bases (`SourceError`,
> `ArtifactError`), restoring symmetry with `NotebookNotFoundError` which has
> mixed in `RPCError` since the 0.5.x series. Combined with the new
> `NotFoundError` umbrella (see **Added** below), the class declarations are
> now:
>
> ```python
> class NotebookNotFoundError(NotFoundError, RPCError, NotebookError): ...
> class SourceNotFoundError(NotFoundError, RPCError, SourceError): ...        # new RPCError mixin in 0.6.0
> class ArtifactNotFoundError(NotFoundError, RPCError, ArtifactError): ...    # new RPCError mixin in 0.6.0
> ```
>
> **Migration.** Code that catches the broad `RPCError` *before* a more
> specific `SourceNotFoundError` / `ArtifactNotFoundError` clause now routes
> "not found" through the broad branch instead of falling through to the
> specific one. Reorder your `except` clauses so the more specific exceptions
> come first.
>
> The example below uses `client.sources.get_fulltext(...)`, which raises
> `SourceNotFoundError` for a missing source. (`client.sources.get(...)`
> returns `None` and does not raise, so it doesn't demonstrate the change.)
>
> ```python
> # BEFORE — in 0.5.x this layout worked: SourceNotFoundError was NOT an
> # RPCError, so it fell through the broad `except RPCError` to the specific
> # handler. In 0.6.0 the broad handler catches it first, leaving the
> # specific `except SourceNotFoundError` clause unreachable.
> try:
>     fulltext = await client.sources.get_fulltext(notebook_id, source_id)
> except RPCError as e:        # ← in 0.6.0 this also catches SourceNotFoundError
>     handle_rpc_failure(e)
> except SourceNotFoundError:  # ← in 0.6.0 this branch becomes unreachable
>     handle_missing_source()
>
> # AFTER — put the specific exception first so the broad branch only sees
> # other RPC failures.
> try:
>     fulltext = await client.sources.get_fulltext(notebook_id, source_id)
> except SourceNotFoundError:
>     handle_missing_source()
> except RPCError as e:
>     handle_rpc_failure(e)
> ```
>
> Code that catches `SourceNotFoundError` / `ArtifactNotFoundError` directly,
> or catches via the domain bases (`SourceError`, `ArtifactError`), or via the
> shared `NotebookLMError` base, continues to behave exactly as before. Only
> the `RPCError`-before-specific ordering is affected.
>
> `SourceNotFoundError.__init__` and `ArtifactNotFoundError.__init__` also
> now accept keyword-only `method_id` / `raw_response` parameters (forwarded
> to the `RPCError` parent), matching the `NotebookNotFoundError` signature.
> All positional call sites remain source-compatible.

- **`notebooklm source stale <ID>` now follows the standard CLI exit-code convention by default.** Exit `0` indicates the freshness check succeeded (regardless of whether the source is fresh or stale); exit `1` indicates an error. Previously the command used an inverted predicate (`0` = stale, `1` = fresh) so the shell idiom `if notebooklm source stale ID; then refresh; fi` worked naturally. **Migration:** scripts that depended on the inverted predicate can opt back into the legacy semantics with the new `--exit-on-stale` flag (`if notebooklm source stale --exit-on-stale ID; then refresh; fi`). Scripts written for the new default should branch on the JSON `stale`/`fresh` fields or stdout text. See [`docs/cli-exit-codes.md`](docs/cli-exit-codes.md#notebooklm-source-stale-source_id--opt-in-inverted-predicate) for the full rationale + the new `Exit code semantics` summary.
- **`NotebookLMClient.rpc_call(...)` no longer accepts `source_path`, `_is_retry`, or `operation_variant`** — the three kwargs deprecated in v0.5.0 (`docs/deprecations.md`) were removed after one MINOR cycle. The public escape hatch's primary contract (`client.rpc_call(method, params)`) is unchanged and the default-shape call keeps working with no migration. Migration:
  - **Keyword callers**: drop the removed kwarg from the call. The previous default-shape behavior (`source_path="/"`, `_is_retry=False`, `operation_variant=None`) is now what every call gets unconditionally — `source_path` was a leaky internal seam, `_is_retry` was an internal retry-loop flag, and `operation_variant` is part of the mutating-RPC idempotency registry. Calls that genuinely needed a non-`"/"` `source_path` or a specific `operation_variant` were already on the wrong layer; build a typed method on a sub-client instead, or open an issue describing the workflow.
  - **Positional callers** (rare): the positional order of the remaining parameters is `(method, params, allow_null, *, disable_internal_retries=...)`, so a previously-positional `source_path` / `_is_retry` argument now binds to a different parameter slot. A pre-cut `client.rpc_call(method, params, "/", True)` (which passed `source_path="/"`, `allow_null=True`) becomes `client.rpc_call(method, params, allow_null=True)` after the cut — switch to keyword arguments for `allow_null` to avoid this footgun.
  - There is no public replacement for the removed internal-only kwargs (`_is_retry`, `operation_variant`); they were never part of the supported surface in the first place.
- **`source add --url` rejects internal hosts by default (SSRF guard).** `localhost`, `127.0.0.1`, RFC-1918, and link-local URLs — and any non-`http(s)` scheme — are now refused before ingestion. **Migration:** pass the new `--allow-internal` flag to ingest an internal `http(s)` URL intentionally (the scheme allowlist still applies). Full detail in **Security** below ([#1114](https://github.com/teng-lin/notebooklm-py/pull/1114)).
- **`source` CLI `--json` output shape changed.** `source get --json` now emits the bare kind value (`"type": "url"`) instead of the leaked Python enum repr (`"type": "SourceType.URL"`), and `source fulltext --json` emits a fixed `{source_id, title, kind, content, url, char_count}` payload instead of a raw `asdict(SourceFulltext)` dump. **Migration:** `--json` consumers parsing `source get`'s `type` field, or relying on extra `fulltext` keys, must update. Full detail in **Fixed** below ([#1129](https://github.com/teng-lin/notebooklm-py/pull/1129)).
- **Post-parse CLI validation errors exit `1` (was `2`) and print a JSON envelope on stdout under `--json`.** For `download` flag conflicts, `generate` validation, `research wait --cited-only`, and `ask --new` + `--conversation-id`, a `--json` invocation now emits `{"error": true, "code": "VALIDATION_ERROR", ...}` on stdout and exits `1` instead of Click's stderr usage text + exit `2`. Text-mode behavior is unchanged. **Migration:** automation parsing these `--json` failures should branch on exit `1` + the JSON body. Full detail in **Changed** below (ADR-015; [#1112](https://github.com/teng-lin/notebooklm-py/pull/1112), [#1115](https://github.com/teng-lin/notebooklm-py/pull/1115), [#1117](https://github.com/teng-lin/notebooklm-py/pull/1117)).

### Added

- **`notebooklm source stale --exit-on-stale` flag** — opt-in back-compat for the legacy inverted-predicate exit codes (`0` = stale, `1` = fresh). The default behavior is now the standard CLI convention (see **Breaking changes** above); pass `--exit-on-stale` to keep `if notebooklm source stale --exit-on-stale ID; then refresh; fi` shell idioms working.
- **`Exit code semantics` summary section in [`docs/cli-exit-codes.md`](docs/cli-exit-codes.md#exit-code-semantics).** A normative one-line table — `0` = succeeded as documented, `1` = failed or queried target not found, `2` = Click parser-time error — backing the convention every command obeys outside the documented intentional exceptions. Cross-references the existing tables and [ADR-015](docs/adr/0015-json-envelope-contract-for-post-parse-click-exceptions.md).
- **`NotFoundError` cross-domain umbrella exception.** Catch `NotFoundError` to handle any "resource not found" case across notebooks, sources, and artifacts in one `except` clause — replacing `except (NotebookNotFoundError, SourceNotFoundError, ArtifactNotFoundError):`. `NotebookNotFoundError`, `SourceNotFoundError`, and `ArtifactNotFoundError` all inherit from `NotFoundError`. The umbrella itself is additive; the asymmetric inheritance noted on its original introduction has been resolved in the same release — all three subclasses also mix in `RPCError` (see **Breaking changes** above for the `except`-ordering migration).
- **`notebooklm notebook delete --json`** ([#1167](https://github.com/teng-lin/notebooklm-py/issues/1167)). `notebook delete` was the last delete command (and the only `list` / `create` / `metadata` sibling) without a JSON envelope — passing `--json` crashed with `No such option`. It now emits the typed success/cancel envelope, refuses to prompt in `--json` mode (requiring `--yes`, else a `VALIDATION_ERROR` envelope + exit `1`), and surfaces `context_cleared: true` when the deleted notebook was the active context ([#1193](https://github.com/teng-lin/notebooklm-py/pull/1193)).
- **`notebooklm skill install --dry-run` / `--no-clobber` / `--force`** ([#1109](https://github.com/teng-lin/notebooklm-py/pull/1109)). Project-scope installs now classify each target as create / up-to-date / overwrite. A target that would be overwritten with *different* content exits `1` and lists the conflicts unless `--force` (overwrite) or `--no-clobber` (skip differing, still create missing) is passed; `--dry-run` previews intended writes without touching disk. Writes go through an atomic temp-file + `os.replace` so a crash can't leave a partial `SKILL.md`. User scope keeps the historical always-overwrite behavior (the new flags error when paired with `--scope user`).
- **`GenerationStatus.is_removed` + `status="removed"`** ([#1168](https://github.com/teng-lin/notebooklm-py/issues/1168)). A delisted or quota-removed artifact now reports `status="removed"` (`is_removed=True`) instead of a synthesized `"failed"`, so callers can distinguish a transient list omission from a server-marked FAILED artifact. `is_failed` stays `False` for a removal; `is_rate_limited` still treats a quota-worded removal as retryable, and CLI exit behavior is unchanged ([#1195](https://github.com/teng-lin/notebooklm-py/pull/1195)).
- **Structured media-timeout diagnostics.** When an *accepted* media task (audio / video / cinematic-video / infographic / slide-deck) stays queued or running past the `--wait` / `wait_for_completion` budget, the artifact APIs now raise a typed timeout exception that preserves the last poll-status transition and media-not-ready metadata (also surfaced in `--json`) instead of a bare timeout ([#1094](https://github.com/teng-lin/notebooklm-py/issues/1094)).

### Changed

- **Media `--wait` default timeouts raised.** `generate audio --wait` now defaults to 1200 s ([#1140](https://github.com/teng-lin/notebooklm-py/pull/1140)) and the video / cinematic-video wait defaults were increased to match empirical generation durations ([#1088](https://github.com/teng-lin/notebooklm-py/pull/1088), [#1094](https://github.com/teng-lin/notebooklm-py/issues/1094)), so long generations no longer time out before the artifact is ready under default settings. `docs/` now documents the media wait budgets and the manual `artifact wait` recovery path.
- **`notebooklm doctor` exits `1` when any check fails** ([#1160](https://github.com/teng-lin/notebooklm-py/issues/1160)). It previously built `status: "fail"` entries but always exited `0`, so CI health checks, `set -e` scripts, and monitoring probes read a broken install as green. Overall health is now computed from the final check states (after any `--fix`) and the process exits `1` if any check still fails (warnings stay non-fatal). The exit happens after the payload/table is emitted, so machine-readable `--json` output is unaffected; `doctor` profile JSON errors are also now wrapped in the typed envelope ([#1179](https://github.com/teng-lin/notebooklm-py/pull/1179), [#1146](https://github.com/teng-lin/notebooklm-py/pull/1146)).
- **Post-parse CLI validation errors emit the typed JSON envelope under `--json`** ([ADR-015](docs/adr/0015-json-envelope-contract-for-post-parse-click-exceptions.md)). `download` flag conflicts (`--force` + `--no-clobber`, `--latest` + `--earliest`, `--all` + `--artifact`), `generate` validation (cinematic `--format` / `--style` conflicts, invalid `--language` / `NOTEBOOKLM_HL`), `research wait --cited-only` without `--import-all`, and `ask --new` + `--conversation-id` now route through `{"error": true, "code": "VALIDATION_ERROR", ...}` on stdout and exit `1` under `--json`, instead of Click's parser bypassing the envelope to exit `2` with usage text on stderr. Text-mode behavior (usage text, exit `2`) is unchanged. Flagged under **Breaking changes** above for `--json` automation ([#1112](https://github.com/teng-lin/notebooklm-py/pull/1112), [#1115](https://github.com/teng-lin/notebooklm-py/pull/1115), [#1117](https://github.com/teng-lin/notebooklm-py/pull/1117)).

### Fixed

- **`notebooklm artifact delete <id> --json` now requires `--yes` before deleting** ([#1197](https://github.com/teng-lin/notebooklm-py/issues/1197)). Without `--yes`, the command emits the typed `VALIDATION_ERROR` envelope, includes `"deleted": false`, exits `1`, and leaves the artifact untouched, matching the other destructive delete commands.
- **HTML file uploads now fail client-side with a clear validation error** ([#1127](https://github.com/teng-lin/notebooklm-py/issues/1127)). `notebooklm source add ./article.html` and `client.sources.add_file(..., "article.html")` previously reached NotebookLM's upload endpoint as `text/html` and surfaced a cryptic upstream `400 Bad Request`. The upload pipeline now rejects `.html` / `.htm` / `.xhtml` / `.xht` / HTML MIME uploads before registering a source, with guidance to convert the page to `.txt`, `.md`, or `.pdf`.
- **`notebooklm source fulltext -o FILE` no longer silently overwrites existing files** ([#1173](https://github.com/teng-lin/notebooklm-py/issues/1173)). Existing output paths now auto-rename by default (`FILE` -> `FILE (2)`, etc.); pass `--force` to overwrite intentionally or `--no-clobber` to fail when the path already exists.
- **`sources.list()` raises on a malformed `GET_NOTEBOOK` response under strict-decode (the default)** ([#1159](https://github.com/teng-lin/notebooklm-py/issues/1159)). A drifted or error-enveloped response was previously folded into an empty list, so a sync script could conclude every source had vanished and re-add them all. The hand-rolled list-shape checks now honor `NOTEBOOKLM_STRICT_DECODE` (logging the drift warning, then raising `RPCError`); a genuinely empty notebook (a `None` sources slot) still returns `[]`. Set `NOTEBOOKLM_STRICT_DECODE=0` for the legacy warn-and-return-`[]` fallback ([#1178](https://github.com/teng-lin/notebooklm-py/pull/1178)).
- **`client.rpc_call(..., allow_null=True)` raises on method-ID drift and anti-bot walls** ([#1158](https://github.com/teng-lin/notebooklm-py/issues/1158)). The decoder gated its entire null-handling block behind `not allow_null`, so opt-in null callers (`CREATE_ARTIFACT`, `GENERATE_MIND_MAP`, `DELETE_SOURCE`, `GET_SUGGESTED_REPORTS`, …) silently received `None` when Google rotated a method ID or served a redirect / anti-bot page. An absent RPC ID (drift) and a body with no RPC frames (anti-bot wall) now always raise; only a present-but-`null` `wrb.fr` frame returns `None`. Null-result error messages now embed the discovered `found_ids` ([#1176](https://github.com/teng-lin/notebooklm-py/pull/1176)).
- **Auth-refresh replay no longer re-issues non-idempotent writes** ([#1157](https://github.com/teng-lin/notebooklm-py/issues/1157)). After a mid-flight auth error (HTTP 401/403, or an auth-shaped decoded `RPCError`) on a probe-then-create method (`CREATE_NOTEBOOK`, `CREATE_ARTIFACT`, `CREATE_NOTE`, `ADD_SOURCE`, `SHARE_NOTEBOOK`, `GENERATE_MIND_MAP`), the refresh-and-retry path could duplicate the resource, invite email, or generation quota when the error landed *after* the server committed the write. Both replay paths (the `AuthRefreshMiddleware` 401/403 leg and the `RpcExecutor` decode-time leg) now honor the effective `disable_internal_retries` classification and propagate the original auth error so the caller's probe-then-create wrapper can disambiguate a commit-lost write ([#1177](https://github.com/teng-lin/notebooklm-py/pull/1177)).
- **`client.notes.create` raises when `CREATE_NOTE` returns no usable note id** ([#1162](https://github.com/teng-lin/notebooklm-py/issues/1162)). It previously fell through to a success-shaped `Note(id="")` that was never finalized via `UPDATE_NOTE` or persisted server-side, so any later operation keyed on the empty id silently misbehaved. It now raises `RPCError`, matching the sibling `add_source` / `notebooks.create` paths ([#1186](https://github.com/teng-lin/notebooklm-py/pull/1186)).
- **Stale authed-POST envelope rebuilt after a `401 → refresh → 429 → retry` flow** ([#1096](https://github.com/teng-lin/notebooklm-py/pull/1096)). The terminal freshness guard's snapshot-equality short-circuit could POST the pre-refresh URL / headers / body against the refreshed cookie jar; the envelope is now rebuilt from a freshly captured auth snapshot on every terminal attempt (byte-identical on the happy path, load-bearing on the post-refresh retry).
- **`NotebookLMClient.close(drain=True)` no longer hangs on in-flight artifact polls** ([#1161](https://github.com/teng-lin/notebooklm-py/issues/1161)). Registered drain hooks (which cancel polls parked in `operation_scope`) now fire before the drain wait, so `close()` short-circuits a pending poll instead of blocking up to the poll's own 300 s timeout ([#1182](https://github.com/teng-lin/notebooklm-py/pull/1182)).
- **`Kernel.open()` closes the httpx client if the open-time cookie snapshot raises** ([#1163](https://github.com/teng-lin/notebooklm-py/issues/1163)). A failure while capturing the open snapshot previously propagated with a live, never-closed client (Python skips `__aexit__` after a failed `__aenter__`), leaking the connection pool. `open()` now `aclose()`s the partial client and resets it so a retry rebuilds cleanly ([#1187](https://github.com/teng-lin/notebooklm-py/pull/1187)).
- **RPC concurrency semaphore gains the loop-affinity guard + close→reopen reset its siblings already have** ([#1169](https://github.com/teng-lin/notebooklm-py/issues/1169)). The per-client `max_concurrent_rpcs` semaphore was the only loop-bound primitive without an affinity guard or reset, so reopening a capped client on a different event loop reused the stale semaphore and could raise "bound to a different event loop" or mispark waiters on Python 3.10/3.11 (masked on 3.12+). It is now guarded by the bound-loop assertion and discarded on any bound-loop change ([#1196](https://github.com/teng-lin/notebooklm-py/pull/1196)).
- **New conversations are serialized per notebook** ([#1144](https://github.com/teng-lin/notebooklm-py/pull/1144)). Concurrent `chat.ask()` calls with no `conversation_id` against the same notebook are serialized so they no longer race to create duplicate server-side conversations.
- **Auth-refresh lock released if the lock-wait metric raises** ([#1164](https://github.com/teng-lin/notebooklm-py/issues/1164)). `await_refresh` recorded the lock-wait metric between `acquire()` and the `try`, so a metric-side exception left the auth-refresh lock held forever, deadlocking every subsequent refresh. The metric call moved inside the `try` / `finally: release()`, matching the sibling `update_auth_tokens` ([#1188](https://github.com/teng-lin/notebooklm-py/pull/1188)).
- **Source-upload registration fails closed on an unparseable source id** ([#1143](https://github.com/teng-lin/notebooklm-py/pull/1143)). The resumable-upload path now raises instead of silently accepting a response it can't parse a source id from, while still tolerating the legacy filename-first row shapes.
- **Artifact-generation defaults and null responses hardened** ([#1063](https://github.com/teng-lin/notebooklm-py/issues/1063), [#1088](https://github.com/teng-lin/notebooklm-py/pull/1088)). Omitting infographic options on the Python `client.artifacts.generate_*` calls now sends concrete visual defaults (matching the CLI) instead of producing a null `CREATE_ARTIFACT` result, and a null artifact-generation response is now classified as `ArtifactFeatureUnavailableError`.
- **`source` command `--json` output shape corrected and stabilized** ([#1129](https://github.com/teng-lin/notebooklm-py/pull/1129)). `source get --json` previously leaked the Python enum repr (`"type": "SourceType.URL"`) and now emits the bare kind value (`"type": "url"`); `source fulltext --json` now emits a fixed `{source_id, title, kind, content, url, char_count}` payload instead of a raw `asdict(SourceFulltext)` dump, and its `-o` envelope gains a `kind` field. **`--json` consumers that parsed `source get`'s `type` field or relied on extra `fulltext` keys must update** (flagged under **Breaking changes** above). Shared serializers keep the shape consistent across the source subcommands going forward.
- **`notebooklm source add -` (stdin) rejects a non-text `--type`.** Piping content from stdin with an explicit non-text source type now fails with a clear validation error instead of mis-routing the content.
- **`notebooklm agent show` routes errors to stderr** ([#1175](https://github.com/teng-lin/notebooklm-py/issues/1175)) so they no longer pollute stdout.
- **Auth-error classification hardened** ([#1142](https://github.com/teng-lin/notebooklm-py/pull/1142)) — empty RPC code labels no longer slip past the auth-error matcher.
- **Malformed `batchexecute` chunk records are now counted** ([#1141](https://github.com/teng-lin/notebooklm-py/pull/1141)) rather than silently dropped, so the `client.metrics` surface reflects partial-response drift.

### Removed

- `NotebookLMClient.rpc_call(source_path=...)`, `NotebookLMClient.rpc_call(_is_retry=...)`, `NotebookLMClient.rpc_call(operation_variant=...)` — see Breaking changes above. The corresponding `DeprecationWarning` emitters in `client.py` and the `tests/unit/test_rpc_call_public_surface.py` warning-surface tests were retired in the same change.

### Security

- **SSRF guard on `source add --url`** ([#1114](https://github.com/teng-lin/notebooklm-py/pull/1114)). The prefix-only `startswith(("http://", "https://"))` check was replaced with a structural `urlsplit` parse + scheme allowlist (`http` / `https` only) plus a private / loopback / link-local IP guard and a `localhost`-literal guard. **Behavior change:** `http://localhost`, `http://127.0.0.1`, RFC-1918 hosts, and `http://169.254.169.254` are now rejected by default — pass the new `--allow-internal` flag to ingest an internal URL intentionally (the scheme allowlist still applies). DNS is never resolved at validation time. Flagged under **Breaking changes** above.
- **Resumable upload URLs validated and redacted** ([#1130](https://github.com/teng-lin/notebooklm-py/pull/1130)). The server-returned upload session / cancel URLs are validated before use and redacted in error and log output so a credentialed upload URL can't leak.
- **Artifact download allowlist validated by hostname** ([#1172](https://github.com/teng-lin/notebooklm-py/issues/1172)). Download host-allowlisting now parses the URL hostname structurally instead of matching a string prefix, closing a bypass where a crafted URL (including encoded-slash hosts, hardened further in [#1199](https://github.com/teng-lin/notebooklm-py/pull/1199)) could satisfy a prefix check while pointing at an untrusted host.
- **`httpx` / `urllib3` logs redacted for library consumers** ([#1166](https://github.com/teng-lin/notebooklm-py/issues/1166)). `configure_logging()` now attaches a logger-level `RedactingFilter` to the `httpx` and `urllib3` loggers at import, so a consumer who enables httpx DEBUG (e.g. `logging.basicConfig(level=logging.DEBUG)`) no longer sees the session id in `?f.sid=...` request lines. Pure defense-in-depth — no handler is added, so consumers who never enable those loggers see no behavior change ([#1191](https://github.com/teng-lin/notebooklm-py/pull/1191)).
- **Bare CSRF / session-id token values redacted in logs** ([#1165](https://github.com/teng-lin/notebooklm-py/issues/1165)). The scrubber now redacts bare `SNlM0e` (CSRF) and `FdrFJe` (session-id) `WIZ_global_data` markers, the `csrf=` form alias, and standalone `AF1_QpN-` CSRF tokens — credential-equivalent shapes that previously passed through `scrub_secrets()` unredacted ([#1189](https://github.com/teng-lin/notebooklm-py/pull/1189)).
- **Playwright login subprocess output sanitized** ([#1111](https://github.com/teng-lin/notebooklm-py/pull/1111)). `ensure_chromium_installed` now strips ANSI control sequences and redacts inherited environment-variable secret values (including JSON-nested leaves such as `NOTEBOOKLM_AUTH_JSON`) from captured subprocess stderr/stdout before surfacing install diagnostics (meta-audit G4).

## [0.5.0] - 2026-05-23

The first release after the v0.4.x auth cookie lifecycle series. Headline user-facing work: a top-to-bottom CLI UX overhaul (uniform `--json`, exit-code policy, shell completion, stdin pipes, SIGINT-resume), auth and cookie reliability hardening (inline PSIDTS cold-start recovery, fail-closed `notebooklm use`, concurrent-upload safety), and the v0.3-era deprecation removal cycle. **Read Breaking changes below before upgrading.**

### Breaking changes

Items that need attention when upgrading from 0.4.x. Full migration prose lives in the natural sections below.

- **`NOTEBOOKLM_STRICT_DECODE` now defaults to `1`** — RPC shape drift raises `UnknownRPCMethodError` (subclass of `RPCError`) at the decoder boundary instead of warning and returning `None`. Set `=0` to opt back into the legacy behavior for one release window (the soft-mode fallback itself now emits `DeprecationWarning` and is scheduled for removal in v0.6.0).
- **`rate_limit_max_retries` default raised from `0` to `3`** with exponential-backoff fallback. Programmatic users now inherit smart-retry behavior matching the CLI. Pass `rate_limit_max_retries=0` to restore the previous immediate-`RateLimitError` behavior. Mutating create RPCs already opt out via `disable_internal_retries=True`.
- **`server_error_max_retries` default raised from `0` to `3`** with the same exponential-backoff fallback, covering HTTP 5xx + retryable network errors (#629). Pass `server_error_max_retries=0` to restore immediate failure on 5xx.
- **`max_concurrent_rpcs` semaphore added with default `16`** (#630). High-fan-out callers (e.g. `asyncio.gather` over 100 RPCs) are now throttled by default instead of saturating the connection pool. Pass `max_concurrent_rpcs=None` to restore unbounded fan-out. Must satisfy `max_concurrent_rpcs <= ConnectionLimits.max_connections`.
- **`notebooklm use <id>` fails closed when the notebook doesn't exist.** `use` now verifies the id with `NotebooksAPI.get(id)` before persisting and exits `1` without writing to `context.json` on a missing notebook / wire failure / auth-expiry. Pass `--force` to bypass verification. `NotebookNotFoundError` now inherits from both `RPCError` and `NotebookError`.
- **`source get` / `artifact get` / `note get` exit `1` on not-found (was `0`).** Matches the rest of the CLI's user-error convention so scripts can branch on the exit code. `--json` failure body uses the standard `{"error": true, "code": "NOT_FOUND", ...}` envelope.
- **`generate cinematic-video --format <non-cinematic>` exits `2` with a UsageError** instead of silently overriding the conflict. Drop the conflicting flag, or use `generate video --format <value>` if a non-cinematic format was intended.
- **`NOTEBOOKLM_REFRESH_CMD` defaults to `shell=False`** (security hardening for the shell-injection footgun when the env var is sourced from CI configs). Now parsed with `shlex.split` and invoked with `subprocess.run(argv, shell=False, ...)`. Set `NOTEBOOKLM_REFRESH_CMD_USE_SHELL=1` (literal `"1"` only) to opt back into the legacy `shell=True`.
- **`source add` no longer follows symlinks by default.** A workspace symlink like `~/Downloads/foo.pdf → /etc/passwd` previously resolved and uploaded the target with no warning. The path now refuses symlink traversal with a `ClickException` (exit `1`) unless `--follow-symlinks` is explicit. Scripts that point at symlink-resolved paths must add the flag (#476).
- **YouTube cookies no longer scraped or trusted by default at login / refresh.** The cookie-domain allowlist split into REQUIRED (NotebookLM + Drive + RotateCookies) and OPTIONAL (YouTube / Docs / Mail / myaccount). Pass `--include-domains=youtube` (or `=all`) on `login` / `auth refresh --browser-cookies <browser>` / `auth inspect` to opt YouTube back in; pass `=docs`/`=mail`/`=myaccount` to opt those sibling domains in explicitly (#483).
- **Artifact generation without `language=` now honors the configured language.** The Python `client.artifacts.generate_*` methods now resolve omitted `language` via `NOTEBOOKLM_HL` / global config / `"en"` instead of hard-coding `"en"` at the signature. Pass `language="en"` for a fixed English payload.
- **`--storage <path>` no longer shares the default profile's notebook context.** A previously-run `notebooklm use <id>` against the default profile is invisible to a later `notebooklm --storage X.json <cmd>` (and vice versa) because `--storage` now derives a sibling `<path>.context.json`. Set the active notebook explicitly via `notebooklm --storage <path> use <id>`, `-n/--notebook`, or `NOTEBOOKLM_NOTEBOOK` env var (#467).
- **`login --browser-cookies --account EMAIL` now writes the active/default profile by default** instead of creating a profile from the email local-part. Use `--profile-name NAME` to write a separate named profile, or `--storage PATH` for an exact file. Existing profile auth for a different or unknown account prompts before overwrite (#987).
- **v0.3-era deprecated APIs removed** — `Source.source_type`, `Artifact.artifact_type`, `Artifact.variant`, `SourceFulltext.source_type`, `StudioContentType`, `DEFAULT_STORAGE_PATH`, `notebooklm.cli.language.save_config`. Migrate to the `.kind` property and `notebooklm.paths.get_storage_path()`. See **Removed** below.
- **Cookie identity widened to `(name, domain, path)`** per RFC 6265 §5.3. Writes remain backward-compatible (flat dicts / legacy 2-tuples still accepted); reads of `auth.cookies` with the old 2-tuple key now raise `KeyError`. Use `auth.cookies[("SID", ".google.com", "/")]`, `auth.flat_cookies["SID"]`, or `auth.cookie_header`.

### Added

#### Auth and reliability
- **Inline `__Secure-1PSIDTS` cold-start recovery.** When a storage file has `__Secure-1PSID` but no `__Secure-1PSIDTS`, a preflight POST to `accounts.google.com/RotateCookies` mints a fresh token before any RPC traffic, so cold-start workers no longer fail on the first call. Cross-process flock serializes concurrent cold starts; respects `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1` ([#865](https://github.com/teng-lin/notebooklm-py/issues/865), [#872](https://github.com/teng-lin/notebooklm-py/pull/872)).
- **`NOTEBOOKLM_BASE_URL` env var for enterprise NotebookLM deployments** (#402). Routes RPC + auth traffic through a non-`google.com` base URL; cookie-domain allowlist auto-extends to the enterprise host. Previously enterprise users had to monkey-patch internals.
- **`NOTEBOOKLM_RPC_OVERRIDES` env-var escape hatch.** When Google rotates a `batchexecute` method ID, set e.g. `NOTEBOOKLM_RPC_OVERRIDES='{"LIST_NOTEBOOKS": "newId123"}'` to keep working until a patch ships. Overrides are gated to `notebooklm.google.com` / `accounts.google.com` base hosts so a redirected base can't pivot them (#486).
- **`ConnectionLimits` dataclass for httpx pool tuning.** Pass `ConnectionLimits(max_connections=200, ...)` to `NotebookLMClient(...)` for long-running agents and high-fan-out workers — no more monkey-patching internals (#527).
- **`max_concurrent_rpcs` constructor arg** (default `16`, #630). Bounds simultaneous in-flight RPCs to protect the connection pool under fan-out. `None` opts out — see **Breaking changes** for the default-shift note.
- **`--include-domains` flag on `login` / `auth refresh --browser-cookies <browser>` / `auth inspect`.** Backs the REQUIRED/OPTIONAL cookie-domain split described in **Breaking changes** — passing `=youtube`/`=docs`/`=mail`/`=myaccount` (or `=all`) opts those OPTIONAL domains back in. Accepts repeated-flag or comma-separated syntax (#483).
- **In-memory `__Secure-1PSIDTS` recovery during `--browser-cookies` extraction** ([#990](https://github.com/teng-lin/notebooklm-py/pull/990), [#991](https://github.com/teng-lin/notebooklm-py/pull/991)). When `rookiepy` returns a partial cookie set (most often when the browser hasn't rotated `__Secure-1PSIDTS` yet), a single `RotateCookies` POST against the live browser cookies mints the missing token before persistence. Recovery declines surface scenario-specific hints (`No SID → "You are not signed in to Google in <browser>"`, `PSIDTS missing + secondary binding intact → "RotateCookies recovery did not succeed. Open https://notebooklm.google.com in <browser>"`) instead of the previous generic "No valid Google authentication cookies found".

#### Chat
- **`client.chat.delete_conversation(notebook_id, conversation_id)` + `notebooklm ask --new` is now genuinely destructive** (#824). Captures the web UI's "Delete history" action (`J7Gthc` RPC) so callers can force-end a server-side conversation; the next `ask()` with no `conversation_id` starts a brand-new thread. **⚠ Deleted turns are not recoverable.** CLI prompts for confirmation; `--json` implies `--yes`.
- **`notebooklm ask --new` flag** (previously promised in the docstring but undeclared) — starts a fresh conversation, mutually exclusive with `--conversation-id`.
- **`notebooklm ask --timeout`** per-invocation HTTP timeout, mirroring `source add --timeout`.
- **`ChatReference.answer_range` + `.score`** (#686). Every reference now exposes the answer-text span it grounds (start/end char positions) and the model's relevance score — useful for highlighting cited passages and ranking sources.
- **`chat save` preserves inline citation hover anchors** (closes #660, [#675](https://github.com/teng-lin/notebooklm-py/pull/675)). Saved notes retain `[citation]`-style anchors so users can hover-preview the source passage that grounded each claim in the NotebookLM web UI.

#### CLI ergonomics
- **Uniform `--json` envelopes** on every detail and mutating command: `artifact get/rename/delete/poll/export`, eight `source` subcommands (`delete/rename/refresh/clean/get/delete-by-title/add-drive/stale`), `note get/save/create/delete/rename`, `notebooklm configure`, and `notebook use`. Detail commands mirror the underlying dataclasses; mutating commands emit `{"id": ..., "renamed|deleted|exported": true, ...}`.
- **Standard download flag set on `download quiz` / `download flashcards`** — `--all`, `--latest`, `--earliest`, `--name`, `--dry-run`, `--force`, `--no-clobber`, `--json` — so one wrapper script works across every artifact type.
- **Uniform `--timeout` / `--interval`** on `generate <kind> --wait`, `artifact wait`, and `source wait`.
- **`--limit=N` and `--no-truncate` on every `list` command, plus `--no-truncate` on `chat history`.** `chat history --no-truncate` lifts the hardcoded 50-char preview on Question/Answer columns.
- **Shell completion + ID-aware completers.** `notebooklm completion <bash|zsh|fish>` prints a completion script; once sourced, `-n/--notebook`, `-s/--source`, and `-a/--artifact` TAB-complete live IDs from the active profile.
- **SIGINT resume hint on long-running `--wait` ops.** Ctrl-C exits 130 with `Cancelled. Resume with: notebooklm artifact poll <task_id>` (or the parallel `source wait <source_id>`) instead of dumping a `KeyboardInterrupt` traceback. Under `--json`: `{"error": true, "code": "CANCELLED", "resume_hint": "..."}`.
- **Unix `-` stdin convention on `ask`, `note create`, `source add`, and `--prompt-file`.** `echo "what is X?" | notebooklm ask -` and similar pipelines now compose without temp files.
- **`NOTEBOOKLM_NOTEBOOK` env var + global `--quiet` flag.** `NOTEBOOKLM_NOTEBOOK=<id> notebooklm ask "..."` works without `-n/--notebook` or a prior `notebooklm use`. `--quiet` suppresses status output, raises the package logger floor to ERROR, and remains mutually exclusive with `-v/-vv`.
- **`source add` warns when a path-shaped argument doesn't exist.** A typo like `./missin.md` previously fell through to inline-text ingestion silently; an advisory stderr warning now fires before the source is added.
- **`--follow-symlinks` opt-in on `source add`.** See **Breaking changes** above; scripts that point at symlink-resolved paths must add the flag to keep working (#476).
- **`source clean` command** (#261). Bulk-delete failed/stale sources in a notebook; pairs with `source stale` for inspection. Supports `--all`, `--latest`, `--earliest`, `--dry-run`, and `--json`.
- **`notebooklm create --use` flag** (#220, #413). `create --use "title"` makes the new notebook the active context in one step. (Plain `create` no longer auto-switches the context — `--use` is the explicit opt-in.)
- **Chromium-profile selectors on `login --browser-cookies chromium:<profile>`** (#648). Pick a specific Chrome user profile (e.g. `chromium:Profile_1`) instead of always defaulting to the first profile. Useful for users with multiple Google accounts in one browser install.
- **`auth login --update` on `--all-accounts`** (#594). Replaces the stored state for an already-logged-in account instead of refusing on conflict.

#### Python API
- **Source fulltext markdown format.** `client.sources.get_fulltext(..., output_format="markdown")` and `source fulltext -f markdown` (closes #222). Requires the optional `markdownify` extra (`pip install "notebooklm-py[markdown]"`).
- **Public `client.rpc_call(method_id, params)`** (#646). A documented escape hatch for invoking any `batchexecute` RPC method directly when no high-level API wraps it yet. Pairs with `NOTEBOOKLM_RPC_OVERRIDES` for community self-patching while waiting on a fix.
- **Observability hooks + drain API on `NotebookLMClient`** (#643). New `on_rpc_event` callback (per-call timing + status), `client.metrics` snapshot, and `await client.drain()` for graceful shutdown. Designed for long-running agents needing visibility without monkey-patching.
- **Correlation IDs + categorized logging** (#430, #431). Every RPC carries an `X-Correlation-ID` (also surfaced on log records); log records are categorized (`rpc.call`, `rpc.retry`, `auth.refresh`, `upload.chunk`, …) for filtering. Credential redaction now covers every log surface by default.
- **Per-call upload timeouts on `sources.add_file` / `add_drive`** (#618). New `upload_timeout` / `chunk_timeout` keyword args for tuning large-file uploads against slow networks.
- **`ResearchAPI.wait_for_completion(notebook_id, task_id=None, *, timeout=1800, interval=5)`** ([#970](https://github.com/teng-lin/notebooklm-py/pull/970)). Polls until research reaches a terminal state (`completed` / `failed`) or the timeout fires; passes through `task_id` on subsequent polls once the backend assigns one to prevent a later concurrent task from substituting its sources/report. Surfaces a new terminal `failed` status so wait loops no longer spin until timeout after the backend rejects a task.
- **`notebooklm.artifacts.with_rate_limit_retry(callable, *, max_retries=3, ...)`** ([#969](https://github.com/teng-lin/notebooklm-py/pull/969)). Shared retry helper for the `client.artifacts.generate_*` family — catches generation-time `RateLimitError`, honors `retry_after`, and falls back to exponential backoff. Replaces the per-caller try/except/sleep boilerplate previously suggested in `docs/python-api.md`.
- **`__all__` declared on `notebooklm.paths`, `notebooklm.migration`, and `notebooklm.notebooklm_cli`** ([#958](https://github.com/teng-lin/notebooklm-py/pull/958)). ADR-012 marks all three as public modules; `__all__` now pins the exported surface (12 names on `paths`, 3 on `migration`, `cli` + `main` on the CLI entry point) so `from notebooklm.paths import *` is well-defined and the public API compatibility audit can lock it.

### Changed
- **Custom `--storage` downloads now use the selected auth file** ([#838](https://github.com/teng-lin/notebooklm-py/issues/838), [#888](https://github.com/teng-lin/notebooklm-py/pull/888)). `ArtifactDownloadService` previously snapshotted the session's storage path at construction time, so `--storage` overrides applied after construction were silently ignored on download. CLI `--storage` flag and mid-process profile switches are now inherited reliably.
- **`--storage <path>` derives a sibling `<path>.context.json` per file** (#467). Two `--storage` invocations against different files no longer leak notebook state through the default profile. Precedence: explicit `--storage` > profile > legacy home-root. (See **Breaking changes** for the script-impact note.)
- **Conversation IDs are now server-assigned** (#659, #667). `ChatAPI.ask()` returns whatever the server creates instead of minting a local UUID. Previously-saved conversation IDs from a v0.4.x session remain valid against the server.
- **Cross-event-loop reuse fails fast with `RuntimeError`** (#633). One `NotebookLMClient` instance is bound to its `open()`-time event loop; reusing it from a different loop (common in hot-reload servers, worker pools) now raises on the first authed POST instead of failing with cryptic httpx errors.
- **`notebook use` surfaces the typed auth-aware error on expired credentials.** Text mode shows the canonical "Not logged in" walkthrough with the `notebooklm login` remediation; `--json` emits the standard `AUTH_REQUIRED` envelope.
- **`download <type>` exception paths route through the typed error handler.** `--json` is honored on the exception path; `RateLimitError.retry_after` surfaces as both a JSON field and a "Retry after Ns" text line; `AuthError` shows the canonical re-auth hint.
- **`notebooklm login` and `notebooklm auth refresh` no longer leak Python tracebacks on unexpected failures.** Unexpected exceptions become a single friendly line + bug-report URL with exit code `2`; original traceback remains available at `-vv`.
- **`--wait` paths show a transient spinner with elapsed timer** and an empirical typical-duration hint where known (e.g. `typically 30-40 min` for cinematic-video). No-op under `--json`.
- **CLI group docstrings synced with the live registered subcommand set.** `source`, `download`, `artifact`, and `note` group `--help` blocks now enumerate every registered subcommand (previously missed `add-drive`, `add-research`, `clean`, `wait`, `cinematic-video`, `quiz`, `flashcards`, `suggestions`, `rename`).
- **`notebooklm --help` bins five previously-orphaned top-level commands** into primary sections: `auth` → **Session**; `metadata` → **Notebooks**; `agent` / `skill` / `language` → **Command Groups**.
- **`artifact poll` vs `artifact wait` `--help` clarified on ID kind.** `poll <task_id>` straight from `generate`; `wait <artifact_id>` resolved against `artifact list`.
- **First-run profile migration no longer races concurrent invocations** (#478). Previously two `notebooklm` invocations starting under a fresh home (container start-up races, parallel test runs, MCP worker pools) could both run the copy-and-delete migration. Lock waits past 30 s raise a domain-specific `MigrationLockTimeoutError(RuntimeError)`.
- **`RPCError.raw_response` previews capped at 80 chars; `NOTEBOOKLM_DEBUG=1` opts into full body.** Previously embedded a 500-char preview of the upstream response — noisy in CI and capable of leaking large server payloads (#479).
- **`RPCError.rpc_id` and `RPCError.code` deprecations revoked.** Both are now permanent aliases for `method_id` / `rpc_code` — removing exception diagnostic aliases can mask the original exception inside `except` handlers.
- **BREAKING: `note delete --json` without `--yes` and `note rename` lose-the-race now exit `1` (was `0`).** Two parallel surgical fixes to `cli/note.py` matching the broader `--json` exit-code convention (audit P1.T5). `notebooklm note delete <id> --json` without `--yes` now emits `{"error": true, "code": "VALIDATION_ERROR", "message": "Pass --yes to confirm deletion in --json mode", "id": ..., "notebook_id": ...}` + exit `1` (was the same payload as `{deleted: false, error: ...}` + exit `0`). `notebooklm note rename <id> "new"` when the note vanishes between the partial-ID resolve and the underlying `get` (e.g. a concurrent `note delete`) now emits the standard `{"error": true, "code": "NOT_FOUND", "message": "Note not found", "id": ..., "notebook_id": ...}` envelope + exit `1` (was `{renamed: false, error: ...}` + exit `0`). **Migration:** scripts branching on the exit code now correctly catch both misconfigurations; scripts parsing the JSON body must switch from `data["deleted"] == false` / `data["renamed"] == false` checks to `data["error"] == true` (or branch on `data["code"]`).

### Deprecated
- **`await NotebookLMClient.from_storage(...)` form.** `from_storage` now returns an awaitable async-context-manager wrapper that supports both the legacy `async with await NotebookLMClient.from_storage(...) as client:` pattern (and bare `await NotebookLMClient.from_storage(...)`) and the new canonical `async with NotebookLMClient.from_storage(...) as client:` pattern. Awaiting the call emits `DeprecationWarning`; the await form will be removed in v1.0. Migration: drop the `await` keyword from `async with await NotebookLMClient.from_storage(...) as client:` call sites.
- **`NotebookLMClient.rpc_call` kwargs `_is_retry`, `source_path`, `operation_variant`.** Emit `DeprecationWarning`; removal targets v0.6.0.
- **`NotesAPI.create_from_chat`.** Use `ChatAPI.save_answer_as_note`; removal targets v0.6.0.
- **Positional `wait` / `wait_timeout` on `SourcesAPI.add_url` / `add_text` / `add_file` / `add_drive`.** Calls like `client.sources.add_url(nb_id, url, True)` still work in v0.5.0 but emit `DeprecationWarning`; pass `wait=True` / `wait_timeout=...` as keywords. Removal targets v0.6.0. CLI is unaffected.
- **`SourcesAPI.add_file` `mime_type` parameter.** Never wired into the resumable-upload RPC — the server derives MIME from the filename extension. Passing a non-`None` value now emits `DeprecationWarning`; removal targets v0.6.0. The separate `add_drive(..., mime_type=...)` parameter is unaffected.
- **`notebooklm source add --mime-type` on the file-source path.** A no-op when the resolved source type is `file`; using it now emits a stderr deprecation note (suppress via `NOTEBOOKLM_QUIET_DEPRECATIONS=1`). Removal targets v0.6.0. The same flag on `source add-drive` is unaffected.
- **`ArtifactsAPI.wait_for_completion(poll_interval=...)`.** Use `initial_interval=...`; `poll_interval` remains accepted until v0.6.0.
- **`NotebooksAPI.share()`.** Use `client.sharing.set_public()`. Scheduled for removal in a future major release.
- **`NOTEBOOKLM_STRICT_DECODE=0` soft-mode fallback.** Each use emits `DeprecationWarning` naming the decoder source; the soft-mode path is scheduled for removal in v0.6.0.
- **`ResearchAPI.poll(task_id=None)` default on multi-task notebooks.** When multiple research tasks are in flight, `poll()` with no `task_id` now emits `DeprecationWarning` (single-task notebooks: no warning, current behavior preserved). Scheduled for removal in a future major release.

### Removed
- **v0.3-era deprecation cycle complete.** Removed `Source.source_type`, `SourceFulltext.source_type`, `Artifact.artifact_type` (use `.kind`); `Artifact.variant` (use `.kind`, `.is_quiz`, `.is_flashcards`); `notebooklm.StudioContentType` (use `ArtifactType`); `notebooklm.DEFAULT_STORAGE_PATH` (use `notebooklm.paths.get_storage_path()`); `notebooklm.cli.language.save_config` (now private).
- **RPC raw-code `StudioContentType` aliases.** `notebooklm.rpc.types.StudioContentType` and `notebooklm.rpc.StudioContentType` removed; use `ArtifactType` for public code and `ArtifactTypeCode` only for low-level RPC internals.
- **`RPCMethod.DISCOVER_SOURCES` and `RPCMethod.QUERY_ENDPOINT`.** `DISCOVER_SOURCES` was an unused enum entry never exercised by any `client.*` API. `QUERY_ENDPOINT` was an endpoint URL path, not a batchexecute RPC method; use `notebooklm.rpc.get_query_url()` for the configured streamed-chat endpoint.

### Fixed
- **Artifact generation language compatibility restored.** Omitting `language` on public `client.artifacts.generate_*` calls again defaults artifact output to `"en"`; pass `language=None` to opt in to `NOTEBOOKLM_HL` default-language resolution.
- **Source upload auth/MIME routing** ([#984](https://github.com/teng-lin/notebooklm-py/pull/984)). The resumable-upload path skipped a redundant env-auth lookup and now classifies media MIME types case-insensitively; `application/mp4` is included in the media-MIME set so `.mp4` uploads route through the media upload path instead of the generic file path.
- **Source upload rejection with status `3` now hints at the per-notebook source cap** ([#977](https://github.com/teng-lin/notebooklm-py/pull/977)). Previously surfaced as a bare `RPCError`; the error message now suggests checking the notebook source count when the server returns the cap-rejection code.
- **Windows atomic-replace races on cookie/profile writes** ([#983](https://github.com/teng-lin/notebooklm-py/pull/983)). `os.replace` on Windows can transiently fail with `ERROR_ACCESS_DENIED` (5) or `ERROR_SHARING_VIOLATION` (32) when the destination is briefly held open by AV scanners or backup software. Bounded retry with backoff handles the transient cases; persistent failures still surface.
- **IO event-loop blocking and chunked-download throughput** ([#981](https://github.com/teng-lin/notebooklm-py/pull/981)). Sync `Path.resolve()` / `open()` / `os.fstat()` on the upload path are now wrapped in `asyncio.to_thread`, keeping the loop responsive under the upload semaphore on slow filesystems. Chunked downloads use a single dedicated writer thread fed by a bounded `queue.Queue` (≈512 KiB buffered) instead of spawning one `to_thread` call per 64 KiB chunk. A bug where `ArtifactDownloadError` (raised by `download_urls_batch()` for invalid scheme / untrusted host / auth failure / HTML payload) aborted the entire batch instead of landing in `DownloadResult.failed` is also fixed.
- **`notebooklm login --browser-cookies` hardening** ([#974](https://github.com/teng-lin/notebooklm-py/pull/974)). Tightened Chromium account enumeration, cookie-jar normalization, and refresh writes so partial extractions surface a clear error instead of silently writing an incomplete `storage_state.json`. Pairs with the in-memory `__Secure-1PSIDTS` recovery shipped in [#990](https://github.com/teng-lin/notebooklm-py/pull/990) / [#991](https://github.com/teng-lin/notebooklm-py/pull/991).
- **`notebooklm login --browser-cookies` Playwright account metadata** ([#989](https://github.com/teng-lin/notebooklm-py/pull/989)). The Playwright login path now writes account metadata to the profile and validates it on subsequent refresh (rejecting bool-shaped corruption from earlier buggy writes), so `notebooklm auth refresh --all-accounts` and `--account EMAIL` can target the right profile without manual cleanup.
- **Playwright account metadata repair runs after the sync context exits** ([#1000](https://github.com/teng-lin/notebooklm-py/issues/1000), [#1002](https://github.com/teng-lin/notebooklm-py/pull/1002)). `notebooklm login` previously invoked `repair_playwright_account_metadata()` while `sync_playwright()`'s event loop was still active, which raised on `run_async()`. The repair is now deferred until after the Playwright context closes, using the captured page HTML and saved storage path.
- **`source add-research --wait` timeout path** ([#971](https://github.com/teng-lin/notebooklm-py/pull/971)). The CLI service now wraps the research wait in a typed timeout error and surfaces a resumable hint (`notebooklm research poll <task_id>`) instead of hanging until the global request timeout.
- **`notebooklm auth refresh --all-accounts` language sync runs once** ([#976](https://github.com/teng-lin/notebooklm-py/pull/976)). Previously re-issued the `notebooklm.SetLanguage` RPC once per account; now coalesces to a single sync at the end of the multi-account loop.
- **Loop-affinity guard on `sources.add_file` and `client.drain()` admission** ([#952](https://github.com/teng-lin/notebooklm-py/pull/952)). Cross-event-loop reuse already failed fast on authed RPC POSTs (#633); upload admission and drain admission now also raise `RuntimeError` instead of silently mis-binding to the wrong loop.
- **`NotebookLMClient.close()` no longer leaks the httpx pool if cancelled mid-drain** ([#950](https://github.com/teng-lin/notebooklm-py/pull/950)). A `CancelledError` raised during drain previously skipped `httpx.AsyncClient.aclose()`; close now shields the transport cleanup so the connection pool is released on every cancellation path.
- **Deep-research source import no longer requires leaving the "Add sources?" modal** ([#315](https://github.com/teng-lin/notebooklm-py/issues/315), [#882](https://github.com/teng-lin/notebooklm-py/pull/882)). The deep-research flow used to discover sources but skip the modal-confirm step, leaving sources pending until a separate UI action committed them. The CLI / `ResearchAPI.import_sources` now commits directly.
- **`DELETE_NOTE` no longer races shielded `UPDATE_NOTE` at cancel time** ([#876](https://github.com/teng-lin/notebooklm-py/pull/876)). Cancellation during an in-flight `NotesAPI.update(...)` could land a delete before the shielded write completed, then have the update resurrect the note. Cancel-time cleanup is now ordered so `DELETE_NOTE` waits for any shielded `UPDATE_NOTE` to settle.
- **Client close preserves the original exception** (#526). `NotebookLMClient.__aexit__` previously masked the original body exception when `aclose()` itself raised. Body exceptions are now preserved (chained via `__cause__`) while close-time failures still propagate; an inner shield guarantees the underlying httpx client is closed on every path.
- **Unique temp file per concurrent artifact download** (#523). Two parallel `download_*` calls against the same artifact used to share `<dest>.tmp` and clobber each other's bytes. Each invocation now allocates a unique temp file (PID + uuid suffix) and atomically renames into place.
- **`add_file` TOCTOU fix + `max_concurrent_uploads` knob** (#595). `SourcesAPI.add_file` used to open the source file twice — a path swap between the two opens could substitute a different file into a successful upload. The file is now opened once; the FD is held across size check + registration + upload. New `max_concurrent_uploads: int | None = 4` on `NotebookLMClient` caps simultaneous in-flight uploads (doubles as an FD-exhaustion guard for `asyncio.gather` fan-outs).
- **Research `task_id` cross-wire on concurrent in-flight tasks** (#619). Two research sessions in flight on the same notebook could let `ResearchAPI.poll(notebook_id)` silently return the latest task, mis-attributing source provenance to the caller's task. `poll()` gains an optional `task_id` discriminator; `import_sources()` raises the new `ResearchTaskMismatchError` (subclass of `ValidationError`) when a `research_task_id` on any source disagrees with the caller's `task_id`.
- **`RPCHealth` surfaces `httpx` exception class name on empty error messages** ([#874](https://github.com/teng-lin/notebooklm-py/pull/874)). Some `httpx` exception classes raise with empty `str(exc)`, which previously surfaced as a blank line. Health output now prefixes the class name (e.g. `ConnectTimeout:`).
- **`notebooklm login` install hint stripped the `[browser]` extra** (#416). Rich interpreted `[browser]` as a style tag, so the "Playwright not installed" message rendered as `pip install "notebooklm-py"` with no extras. Fixed by `markup=False`; also corrected the package name from `notebooklm` to `notebooklm-py`.
- **Per-create-RPC idempotency hardening** ([#801](https://github.com/teng-lin/notebooklm-py/pull/801), [#806](https://github.com/teng-lin/notebooklm-py/pull/806), [#808](https://github.com/teng-lin/notebooklm-py/pull/808), [#809](https://github.com/teng-lin/notebooklm-py/pull/809), [#813](https://github.com/teng-lin/notebooklm-py/pull/813)). Six-policy idempotency registry with probe-then-retry semantics for `ADD_SOURCE`, `ADD_SOURCE_FILE`, `CREATE_NOTE`, `CREATE_ARTIFACT`, `GENERATE_MIND_MAP`, and `START_RESEARCH` / `IMPORT_SOURCES`. Resolves duplicate-create on transient retries while still raising clear errors for genuine probe failures.

### Security
- **Comprehensive secret-leak audit closed across logging, auth, and URL handling** ([#746](https://github.com/teng-lin/notebooklm-py/pull/746), [#803](https://github.com/teng-lin/notebooklm-py/pull/803), [#903](https://github.com/teng-lin/notebooklm-py/pull/903)). A multi-iteration sweep tightening every surface that could leak credentials or grant codes:
  - `payload_preview`, `final_url`, and share-URL IDs scrubbed in error paths (#746).
  - `repr()` redaction on auth objects, `NOTEBOOKLM_REFRESH_CMD` stdout/stderr redaction, Playwright cookie-jar domain filter, atomic profile-state writes (#803).
  - Standalone `__Secure-1PSIDTS` / `__Secure-3PSIDTS` / `__Secure-1PAPISID` / `__Secure-3PAPISID` cookie redaction in `_logging.py` (previously only caught inside `Cookie:` / `Set-Cookie:` header values); `_safe_url` redacts the URL **path** with `/<redacted>` on Google OAuth hosts (`accounts.google.com`, `oauth2.googleapis.com`, `oauth2.googleusercontent.com`) and subdomains, so opaque grant codes in paths like `/o/oauth2/auth/<token>` no longer leak through `ValueError` interpolations or CSRF / session-id drift surfaces (#903).

## [0.4.1] - 2026-05-11

> **Compatibility note.** Despite a few additive items (`notebooklm auth refresh` CLI, `keepalive=` constructor argument on `NotebookLMClient`, `NOTEBOOKLM_REFRESH_CMD` env var, two new dataclass fields), 0.4.1 is shipped as a patch release because the dominant work — and the reason to ship now — is auth/cookie stability remediation. Bumping to v0.5.0 would force the long-deferred removal of v0.3-era deprecated APIs (see [Stability](docs/stability.md)) earlier than scheduled; we'd rather keep that change isolated from the auth cookie lifecycle work. All additive items are backward compatible — existing code keeps working without changes.

### Added
- **`notebooklm auth refresh` CLI command** - One-shot keepalive that opens a session, triggers the layer-1 SIDTS rotation poke against `accounts.google.com`, persists the rotated cookies to `storage_state.json`, and exits. Designed to be scheduled by the OS (launchd / systemd / cron / Task Scheduler / k8s CronJob) to keep an idle profile from staling out between user-driven calls. Pairs naturally with `--quiet` for log-only-on-error cron output. Requires file/profile-backed authentication — explicitly refuses to run when `NOTEBOOKLM_AUTH_JSON` is set (no writable backing store). See `docs/troubleshooting.md` for per-OS scheduler recipes (#336).
- **Periodic keepalive task on `NotebookLMClient`** - Long-lived clients (agents, workers, multi-hour `async with` blocks) can opt into a background task that periodically POSTs `RotateCookies` to drive `__Secure-1PSIDTS` rotation, then persists rotated cookies to `storage_state.json` immediately so a crash doesn't lose the freshness. Disabled by default — pass `keepalive=<seconds>` to `NotebookLMClient(...)` or `NotebookLMClient.from_storage(...)` to enable. Values below `keepalive_min_interval` (default 60 s) are clamped up to that floor. The loop swallows transient errors at DEBUG and continues; cancellation on `__aexit__` is clean. Persistence runs off-loop via `asyncio.to_thread` so the loop never blocks on disk I/O. Closes the gap left by the per-call layer-1 poke for clients that never re-call `fetch_tokens` (#297, #312, #341).
- **Auto-refresh on auth expiry** - `fetch_tokens` now optionally runs a user-provided shell command when a Google session cookie has expired, reloads cookies from the same storage path, and retries once. Opt in by setting the `NOTEBOOKLM_REFRESH_CMD` environment variable to a command that rewrites `storage_state.json` (e.g. a sync script reading from a cookie vault). Refresh commands receive `NOTEBOOKLM_REFRESH_STORAGE_PATH` and `NOTEBOOKLM_REFRESH_PROFILE` so profile-aware scripts can target the active auth file. Covers every CLI entry point without changing the public API. Retry guards prevent refresh loops (#336).
- **`examples/refresh_browser_cookies.py`** - Sample `NOTEBOOKLM_REFRESH_CMD` script that re-extracts cookies from a live local browser via `notebooklm login --browser-cookies`. Provides a recovery path for unattended automation when the in-process keepalive isn't enough (idle gaps, force-logout, password change).
- **`Source.created_at` and `GenerationStatus.url` public dataclass fields** - `Source.created_at` is now populated for both nested and deeply-nested response paths. `GenerationStatus.url` is now populated by `poll_status` for media artifact types (audio, video, infographic, slide-deck PDF) so callers can stream the asset as soon as the status flips to ready (#349, #356).
- **`ALLOWED_COOKIE_DOMAINS` extended for sibling Google products** - The browser-cookie import path now accepts cookies from Google's sibling product domains, restoring `--browser-cookies` flows for users whose active Google session lives on a sibling surface rather than `notebooklm.google.com` directly (#362).

### Fixed
- **Cookies could silently stale out under sustained use** - `fetch_tokens` now POSTs to `https://accounts.google.com/RotateCookies` (Chrome's dedicated unsigned rotation endpoint) before hitting `notebooklm.google.com` to drive `__Secure-1PSIDTS` / `__Secure-3PSIDTS` rotation. Empirically validated against both DBSC-bound (Playwright-minted) and unbound (Firefox-imported) profiles. RPC traffic against `notebooklm.google.com` alone does not appear to trigger rotation, so a keepalive that hit NotebookLM alone could silently stale out. The rotated `Set-Cookie` lands in the live `httpx` jar and is persisted via `save_cookies_to_storage()` along the `fetch_tokens_with_domains` / `AuthTokens.from_storage` paths. A 60 s mtime guard rate-limits the layer-1 poke — the POST is skipped when storage was recently rotated. Failures log at DEBUG and never abort token fetch. Disable with `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1` (e.g. networks that block `accounts.google.com`). Closes #312 (#345, #346).
- **Concurrent `RotateCookies` poke stampede** - The 60 s mtime guard only debounces *sequential* invocations; under `asyncio.gather` fan-out, parallel CLI loops, or MCP worker pools, all callers see the same stale `storage_state.json` mtime and stampede the POST. Three layered protections inside `_poke_session`: a per-event-loop, per-storage-path async lock registry plus a sync state lock for in-process dedup (an `asyncio.gather` of 10 fires exactly one POST), a non-blocking `LOCK_EX | LOCK_NB` flock on the new `.storage_state.json.rotate.lock` sentinel for cross-process dedup (parallel CLI loops / MCP workers skip silently when another process is rotating), and a failure-stampede protection where the timestamp updates regardless of POST outcome — so a 15 s timeout against a hung `accounts.google.com` doesn't let 10 fanned-out callers each wait the full timeout. The layer-2 keepalive loop now calls the bare `_rotate_cookies` directly (it's already self-paced via `keepalive_min_interval`) and `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` continues to disable both layers (#347, #348).
- **`Notebook.sources_count` parsed but never surfaced** - The `sources_count` field on the public `Notebook` dataclass is now populated from `data[1]` on both LIST and GET notebook shapes; previously it always read as `0` regardless of actual source count (#350).
- **`Artifact.url` unpopulated for media artifacts** - The `url` field on the public `Artifact` dataclass is now populated for media types (audio, video, infographic; slide-deck exposes the PDF URL — use `download_slide_deck(output_format="pptx")` for PPTX) so callers no longer need to drop down to `download_*` to obtain the asset URL (#349, #356).
- **Cross-process and refresh-path save races** - Close lifecycle and refresh-path saves now serialize correctly with the keepalive writer; concurrent writers no longer overwrite each other's rotated cookies (#344).
- **Keepalive ↔ close serialization; stop mutating caller `Auth`** - The keepalive task no longer races with `__aexit__`, and no longer mutates the `Auth` instance the caller passed in. Callers that share an `Auth` across multiple clients now get the isolation the API documented (#343).
- **Snapshot keepalive cookie jar; normalize explicit `storage_path`** - The keepalive task now snapshots the live `httpx` jar before writing (avoiding torn writes when an RPC is mid-flight); an explicit `storage_path=` argument to `NotebookLMClient` is normalized onto the `Auth` instance so the keepalive task writes to the file the caller actually pointed at (#342).
- **Per-domain cookie scoping on file upload** - File-upload requests now send only cookies whose `Domain` attribute applies to the upload host, instead of the full jar. Prevents upload rejection when the jar mixes cookies for `google.com`, `notebooklm.google.com`, and `googleusercontent.com` (#373, #374).
- **Two-tier cookie validation pre-flight** - Auth loaders now distinguish "missing-but-recoverable" from "fatal" cookie states before attempting an RPC, surfacing clearer errors and avoiding doomed requests against Google's identity surface (#372).
- **Preserve cookie attributes on load** - `Domain`, `Path`, `Secure`, `HttpOnly`, and `SameSite` attributes round-trip through storage load, restoring behaviors that depended on cross-host scoping (#365, #368).
- **Unify flat-cookie selection across loaders** - Legacy flat-cookie and modern Playwright storage shapes now share a single selection contract; subtle mismatches between the two paths are eliminated (#375, #376).
- **Tolerate non-numeric / out-of-range timestamp values on dataclasses** - `Notebook.created_at`, `Source.created_at`, and `Artifact.created_at` now catch `TypeError`, `ValueError`, `OSError`, and `OverflowError` from `datetime.fromtimestamp` and resolve to `None` instead of raising on edge-case server responses (#357).
- **`examples/refresh_browser_cookies.py` `--profile` placement** - The example invoked `... login --browser-cookies <b> --profile <p>` but `--profile` is a top-level Click option and was rejected after `login` (`Error: No such option: --profile`). Now invokes `... --profile <p> login --browser-cookies <b>` and works end-to-end against profile-backed storage.

### Infrastructure
- **Consolidated URL extraction** - `_extract_artifact_url`, per-type extractors (audio/video/infographic/slide-deck), and `_is_valid_artifact_url` moved to `types.py`. Readiness checks, `Artifact.url`, `GenerationStatus.url`, and the download paths now share one URL-selection contract: `mp4` quality-4 > any `mp4` > first valid URL for video. `SourcesAPI.get_fulltext` fixed for YouTube fulltext URLs at `metadata[5][0]` along the way (#349, #356).
- **Removed redundant `ArtifactsAPI` URL helpers** - Private `_is_valid_media_url` and `_find_infographic_url` shim methods removed; tests now exercise the canonical `types.py` helpers (#358).
- **E2E `--profile` pytest flag** - `pytest --profile <name>` scopes the E2E notebook ID cache to a named profile, so parallel multi-profile test runs don't collide on the cached notebook fixture (#340).

## [0.4.0] - 2026-05-09

### Added
- **Multi-account profiles** - Switch between Google accounts without re-authenticating (#227)
  - `notebooklm profile create/list/switch/rename/delete` commands
  - Global `--profile` / `-p` flag and `NOTEBOOKLM_PROFILE` environment variable to scope any command to a profile
  - Per-profile storage paths under `~/.notebooklm/profiles/<name>/`
  - Implicit default profile preserved for backward compatibility; existing `~/.notebooklm/storage_state.json` is auto-detected as the default profile (no manual migration needed)
- **`notebooklm doctor` diagnostic command** - `notebooklm doctor [--fix] [--json]` checks profile setup, auth, and migration status; reports actionable issues
- **Microsoft Edge SSO login** - `notebooklm login --browser msedge` for organizations that require Edge for SSO (#204)
- **Browser cookie import** - Reuse cookies from your existing browser session without driving Playwright
  - `notebooklm login --browser-cookies <browser>` (chrome, edge, firefox, safari, etc.)
  - New `convert_rookiepy_cookies_to_storage_state()` Python helper
  - Optional `[cookies]` extra installs `rookiepy` (`pip install "notebooklm-py[cookies]"`)
  - Honors the active profile: `notebooklm --profile <name> login --browser-cookies <browser>` writes to that profile's `storage_state.json`. Note that cookie extraction always pulls the source browser's currently-active Google account for `google.com` / `notebooklm.google.com` — to populate multiple profiles from the same browser, switch the active Google account in the browser between runs (or use a separate browser per profile).
- **EPUB source type** - Upload `.epub` files as notebook sources (#231)
- **Agent skill installation** - Install the bundled NotebookLM skill into local AI agents (#206, #207)
  - `notebooklm skill install` - Install into `~/.claude/skills/notebooklm` and `~/.agents/skills/notebooklm`
  - `notebooklm skill status` - Check installation state
  - `notebooklm agent show codex` / `notebooklm agent show claude` - Print bundled agent templates
- **Mind map customization** - `client.artifacts.generate_mind_map()` now accepts `language` and `instructions` parameters (#252)
- **`note list --json`** - Machine-readable note listings (#259)
- **Bare status codes in decoder errors** - Decoder surfaces server status codes on null RPC results for clearer diagnostics (#114, #294)

### Fixed
- **Cross-domain cookie preservation** - Login storage state retains cookies across `google.com` and `notebooklm.google.com` subdomains, restoring sessions for regional domains
- **NotebookLM subdomain cookies** - Subdomain cookies are no longer dropped during login (#334)
- **Video artifact detection** - Correctly detect completed video media URLs in polling responses (#333)
- **Research import on unavailable snapshots** - CLI gracefully handles missing source snapshots during research import (#335)
- **Source import retry** - Filtered partial-import retry payloads and tightened verification to avoid false positives (#321, #327)
- **Server-state verification on timeout** - Prevents duplicate inflation when source imports time out (#319)
- **Playwright navigation interruption** - Handles updated Playwright behavior on already-authenticated sessions (#214, #322)
- **Login subprocess on Windows** - Use `sys.executable` for Playwright subprocess calls (#279)
- **Legacy Windows Unicode output** - Sanitized output streams for legacy Windows consoles (#324)
- **Settings quota errors** - Use account limits when reporting create-quota failures (#328)
- **Chat references** - Emit references only from the winning chunk to avoid >600-element duplication (#300, #310)
- **Login retry mechanism** - Resolved race conditions and improved error handling on retry (#243)
- **Quota detection during polling** - Detect quota / daily-limit failures during artifact polling (#240)
- **Google account switching** - Fixed switching between Google accounts at login time (#246)
- **YouTube URL extraction** - Extract YouTube URLs at deeply-nested response positions (#265)
- **Bare-HTTP URL fallback** - Disabled brittle bare-HTTP fallback in `sources.list()` (#294)
- **Logout context cleanup** - Clear the active notebook context on `notebooklm logout`
- **Infographic URL extraction** - Aligned with download-path logic; added regression test (#229)
- **Custom storage path for downloads** - Artifact downloads now respect custom auth storage paths (#235)
- **Windows file permissions** - Skip Unix-only `0o600` calls on Windows and rely on Python 3.13+ ACL behavior (#225)
- **TOCTOU protection** - Hardened directory creation in `session.py` (#225)

### Changed
- **`rookiepy` is an optional `[cookies]` extra** - Excluded from `[all]` to avoid Python 3.13+ install issues; install with `pip install "notebooklm-py[cookies]"`
- **Login error detection** - Improved detection of missing browser binaries (e.g., `msedge` not installed)
- **Skill installation paths** - Hardened to handle alternative `~/.claude` and `~/.agents` layouts
- **Deprecation removal deferred to v0.5.0** - The deprecated APIs originally scheduled for removal in v0.4.0 — `StudioContentType`, `Source.source_type`, `SourceFulltext.source_type`, `Artifact.artifact_type`, `Artifact.variant`, and `DEFAULT_STORAGE_PATH` — continue to work and emit `DeprecationWarning`. Removal is now planned for v0.5.0 to give downstream users an extra release to migrate.

### Infrastructure
- Pinned `ruff==0.8.6` in dev deps to match pre-commit configuration
- Bumped `python-dotenv` (#299)
- Bumped `pytest` in the `uv` group
- Added contribution templates and PR quality guidelines for issues and PRs

## [0.3.4] - 2026-03-12

### Added
- **Notebook metadata export** - Added notebook metadata APIs and CLI export with a simplified sources list
  - New `notebooklm metadata` command with human-readable and `--json` output
  - New `NotebookMetadata` and `SourceSummary` public types
  - New `client.notebooks.get_metadata()` helper
- **Cinematic Video Overview support** - Added cinematic generation and download flows
  - `notebooklm generate video --format cinematic`
- **Infographic styles** - Added CLI support for selecting infographic visual styles
- **`source delete-by-title`** - Added explicit exact-title deletion command for sources

### Fixed
- **Research imports on timeout** - CLI research imports now retry on timeout with backoff
- **Metadata command behavior** - Aligned metadata output and implementation with current CLI patterns
- **Regional login cookies** - Improved browser login handling for regional Google domains
- **Notebook summary parsing** - Fixed notebook summary response parsing
- **Source delete UX** - Improved source delete resolution, ambiguity handling, and title-vs-ID errors
- **Empty downloads** - Raise an error instead of producing zero-byte files
- **Module execution** - Added `python -m notebooklm` support

### Changed
- **Documentation refresh** - Updated release, development, CLI, README, and Python API docs for current commands, APIs, and `uv` workflows
- **Public API surface** - Exported `NotebookMetadata`, `SourceSummary`, and `InfographicStyle`

## [0.3.3] - 2026-03-03

### Added
- **`ask --save-as-note`** - Save chat answers as notebook notes directly from the CLI (#135)
  - `notebooklm ask "question" --save-as-note` - Save response as a note
  - `notebooklm ask "question" --save-as-note --note-title "Title"` - Save with custom title
- **`history --save`** - Save full conversation history as a notebook note (#135)
  - `notebooklm history --save` - Save history with default title
  - `notebooklm history --save --note-title "Title"` - Save with custom title
  - `notebooklm history --show-all` - Show full Q&A content instead of preview
- **`generate report --append`** - Append custom instructions to built-in report format templates (#134)
  - Works with `briefing-doc`, `study-guide`, and `blog-post` formats (no effect on `custom`)
  - Example: `notebooklm generate report --format study-guide --append "Target audience: beginners"`
- **`generate revise-slide`** - Revise individual slides in an existing slide deck (#129)
  - `notebooklm generate revise-slide "prompt" --artifact <id> --slide 0`
- **PPTX download for slide decks** - Download slide decks as editable PowerPoint files (#129)
  - `notebooklm download slide-deck --format pptx` (web UI only offers PDF)

### Fixed
- **Partial artifact ID in download commands** - Download commands now support partial artifact IDs (#130)
- **Chat empty answer** - Fixed `ask` returning empty answer when API response marker changes (#123)
- **X.com/Twitter content parsing** - Fixed parsing of X.com/Twitter source content (#119)
- **Language sync on login** - Syncs server language setting to local config after `notebooklm login` (#124)
- **Python version check** - Added runtime check with clear error message for Python < 3.10 (#125)
- **RPC error diagnostics** - Improved error reporting for GET_NOTEBOOK and auth health check failures (#126, #127)
- **Conversation persistence** - Chat conversations now persist server-side; conversation ID shown in `history` output (#138)
- **History Q&A previews** - Fixed populating Q&A previews using conversation turns API (#136)
- **`generate report --language`** - Fixed missing `--language` option for report generation (#109)

### Changed
- **Chat history API** - Simplified history retrieval; removed `exchange_id`, improved conversation grouping with parallel fetching (#140, #141)
- **Conversation ID tracking** - Server-side conversation lookup via new `hPTbtc` RPC (`GET_LAST_CONVERSATION_ID`) replaces local exchange ID tracking
- **History Q&A population** - Now uses `khqZz` RPC (`GET_CONVERSATION_TURNS`) to fetch full Q&A turns with accurate previews (#136)

### Infrastructure
- Bumped `actions/upload-artifact` from v6 to v7 (#131)

## [0.3.2] - 2026-01-26

### Fixed
- **CLI conversation reset** - Fixed conversation ID not resetting when switching notebooks (#97)
- **UTF-8 file encoding** - Added explicit UTF-8 encoding to all file I/O operations (#93)
- **Windows Playwright login** - Restored ProactorEventLoop for Playwright login on Windows (#91)

### Infrastructure
- Fixed E2E test teardown hook for pytest 8.x compatibility (#101)
- Added 15-second delay between E2E generation tests to avoid rate limits (#95)

## [0.3.1] - 2026-01-23

### Fixed
- **Windows CLI hanging** - Fixed asyncio ProactorEventLoop incompatibility causing CLI to hang on Windows (#79)
- **Unicode encoding errors** - Fixed encoding issues on non-English Windows systems (#80)
- **Streaming downloads** - Downloads now use streaming with temp files to prevent corrupted partial downloads (#82)
- **Partial ID resolution** - All CLI commands now support partial ID matching for notebooks, sources, and artifacts (#84)
- **Source operations** - Fixed empty array handling and `add_drive` nesting (#73)
- **Guide response parsing** - Fixed 3-level nesting in `get_guide` responses (#72)
- **RPC health check** - Handle null response in health check scripts (#71)
- **Script cleanup** - Ensure temp notebook cleanup on failure or interrupt

### Infrastructure
- Added develop branch to nightly E2E tests with staggered schedule
- Added custom branch support to nightly E2E workflow for release testing

## [0.3.0] - 2026-01-21

### Added
- **Language settings** - Configure output language for artifact generation (audio, video, etc.)
  - New `notebooklm language list` - List all 80+ supported languages with native names
  - New `notebooklm language get` - Show current language setting
  - New `notebooklm language set <code>` - Set language (e.g., `zh_Hans`, `ja`, `es`)
  - Language is a **global** setting affecting all notebooks in your account
  - `--local` flag for offline-only operations (skip server sync)
  - `--language` flag on generate commands for per-command override
- **Sharing API** - Programmatic notebook sharing management
  - New `client.sharing.get_status(notebook_id)` - Get current sharing configuration
  - New `client.sharing.set_public(notebook_id, True/False)` - Enable/disable public link
  - New `client.sharing.set_view_level(notebook_id, level)` - Set viewer access (FULL_NOTEBOOK or CHAT_ONLY)
  - New `client.sharing.add_user(notebook_id, email, permission)` - Share with specific users
  - New `client.sharing.update_user(notebook_id, email, permission)` - Update user permissions
  - New `client.sharing.remove_user(notebook_id, email)` - Remove user access
  - New `ShareStatus`, `SharedUser` dataclasses for structured sharing data
  - New `ShareAccess`, `SharePermission`, `ShareViewLevel` enums
- **`SourceType` enum** - New `str, Enum` for type-safe source identification:
  - `GOOGLE_DOCS`, `GOOGLE_SLIDES`, `GOOGLE_SPREADSHEET`, `PDF`, `PASTED_TEXT`, `WEB_PAGE`, `YOUTUBE`, `MARKDOWN`, `DOCX`, `CSV`, `IMAGE`, `MEDIA`, `UNKNOWN`
- **`ArtifactType` enum** - New `str, Enum` for type-safe artifact identification:
  - `AUDIO`, `VIDEO`, `REPORT`, `QUIZ`, `FLASHCARDS`, `MIND_MAP`, `INFOGRAPHIC`, `SLIDES`, `DATA_TABLE`, `UNKNOWN`
- **`.kind` property** - Unified type access across `Source`, `Artifact`, and `SourceFulltext`:
  ```python
  # Works with both enum and string comparison
  source.kind == SourceType.PDF        # True
  source.kind == "pdf"                 # Also True
  artifact.kind == ArtifactType.AUDIO  # True
  artifact.kind == "audio"             # Also True
  ```
- **`UnknownTypeWarning`** - Warning (deduplicated) when API returns unknown type codes
- **`SourceStatus.PREPARING`** - New status (5) for sources in upload/preparation phase
- **E2E test coverage** - Added file upload tests for CSV, MP3, MP4, DOCX, JPG, Markdown with type verification
- **`--retry` flag for generation commands** - Automatic retry with exponential backoff on rate limits
  - `notebooklm generate audio --retry 3` - Retry up to 3 times on rate limit errors
  - Works with all generate commands (audio, video, quiz, etc.)
- **`ArtifactStatus.FAILED`** - New status (code 4) for artifact generation failures
- **Centralized exception hierarchy** - All errors now inherit from `NotebookLMError` base class
  - New `SourceAddError` with detailed failure messages for source operations
  - Granular exception types for better error handling in automation
- **CLI `share` command group** - Notebook sharing management from command line
  - `notebooklm share` - Enable public sharing
  - `notebooklm share --revoke` - Disable public sharing
- **Partial UUID matching for note commands** - `note get`, `note delete`, etc. now support partial IDs

### Fixed
- **Silent failures in CLI** - Commands now properly report errors instead of failing silently
- **Source type emoji display** - Improved consistency in `source list` output

### Changed
- **Source type detection** - Use API-provided type codes as source of truth instead of URL/extension heuristics
- **CLI file handling** - Simplified to always use `add_file()` for proper type detection

### Removed
- **`detect_source_type()`** - Obsolete heuristic function replaced by `Source.kind` property
- **`ARTIFACT_TYPE_DISPLAY`** - Unused constant replaced by `get_artifact_type_display()`

### Deprecated
The following emit `DeprecationWarning` when accessed and were originally scheduled for removal in v0.4.0.
See [Migration Guide](docs/stability.md#migrating-from-v02x-to-v030) for upgrade instructions.

> **Note:** Removal was subsequently deferred one release; see the [0.4.0] entry above. These names will now be removed in v0.5.0.

- **`Source.source_type`** - Use `.kind` property instead (returns `SourceType` str enum)
- **`Artifact.artifact_type`** - Use `.kind` property instead (returns `ArtifactType` str enum)
- **`Artifact.variant`** - Use `.kind`, `.is_quiz`, or `.is_flashcards` instead
- **`SourceFulltext.source_type`** - Use `.kind` property instead
- **`StudioContentType`** - Use `ArtifactType` (str enum) for user-facing code

## [0.2.1] - 2026-01-15

### Added
- **Authentication diagnostics** - New `notebooklm auth check` command for troubleshooting auth issues
  - Shows storage file location and validity
  - Lists cookies present and their domains
  - Detects `NOTEBOOKLM_AUTH_JSON` and `NOTEBOOKLM_HOME` usage
  - `--test` flag performs network validation
  - `--json` flag for machine-readable output (CI/CD friendly)
- **Structured logging** - Comprehensive DEBUG logging across library
  - `NOTEBOOKLM_LOG_LEVEL` environment variable (DEBUG, INFO, WARNING, ERROR)
  - RPC call timing and method tracking
  - Legacy `NOTEBOOKLM_DEBUG_RPC=1` still works
- **RPC health monitoring** - Automated nightly check for Google API changes
  - Detects RPC method ID mismatches before they cause failures
  - Auto-creates GitHub issues with `rpc-breakage` label on detection

### Fixed
- **Cookie domain priority** - Prioritize `.google.com` cookies over regional domains (e.g., `.google.co.uk`) for more reliable authentication
- **YouTube URL parsing** - Improved handling of edge cases in YouTube video URLs

### Documentation
- Added `auth check` to CLI reference and troubleshooting guide
- Consolidated CI/CD troubleshooting in development guide
- Added installation instructions to SKILL.md for Claude Code
- Clarified version numbering policy (PATCH vs MINOR)

## [0.2.0] - 2026-01-14

### Added
- **Source fulltext extraction** - Retrieve the complete indexed text content of any source
  - New `client.sources.get_fulltext(notebook_id, source_id)` Python API
  - New `source fulltext <source_id>` CLI command with `--json` and `-o` output options
  - Returns `SourceFulltext` dataclass with content, title, URL, and character count
- **Chat citation references** - Get detailed source references for chat answers
  - `AskResult.references` field contains list of `ChatReference` objects
  - Each reference includes `source_id`, `cited_text`, `start_char`, `end_char`, `chunk_id`
  - Use `notebooklm ask "question" --json` to see references in CLI output
- **Source status helper** - New `source_status_to_str()` function for consistent status display
- **Quiz and flashcard downloads** - Export interactive study materials in multiple formats
  - New `download quiz` and `download flashcards` CLI commands
  - Supports JSON, Markdown, and HTML output formats via `--format` flag
  - Python API: `client.artifacts.download_quiz()` and `client.artifacts.download_flashcards()`
- **Extended artifact downloads** - Download additional artifact types
  - New `download report` command (exports as Markdown)
  - New `download mind-map` command (exports as JSON)
  - New `download data-table` command (exports as CSV)
  - All download commands support `--all`, `--latest`, `--name`, and `--artifact` selection options

### Fixed
- **Regional Google domain authentication** - SID cookie extraction now works with regional Google domains (e.g., google.co.uk, google.de, google.cn) in addition to google.com
- **Artifact completion detection** - Media URL availability is now verified before reporting artifact as complete, preventing premature "ready" status
- **URL hostname validation** - Use proper URL parsing instead of string operations for security

### Changed
- **Pre-commit checks** - Added mypy type checking to required pre-commit workflow

## [0.1.4] - 2026-01-11

### Added
- **Source selection for chat and artifacts** - Select specific sources when using `ask` or `generate` commands
  - New `--sources` flag accepts comma-separated source IDs or partial matches
  - Works with all generation commands (audio, video, quiz, etc.) and chat
- **Research sources table** - `research status` now displays sources in a formatted table instead of just a count

### Fixed
- **JSON output broken in TTY terminals** - `--json` flag output was including ANSI color codes, breaking JSON parsing for commands like `notebooklm list --json`
- **Warning stacklevel** - `warnings.warn` calls now report correct source location

### Infrastructure
- **Windows CI testing** - Windows is now part of the nightly E2E test matrix
- **VCR.py integration** - Added recorded HTTP cassette support for faster, deterministic integration tests
- **Test coverage improvements** - Improved coverage for `_artifacts.py` (71% → 83%), `download.py`, and `session.py`

## [0.1.3] - 2026-01-10

### Fixed
- **PyPI README links** - Documentation links now work correctly on PyPI
  - Added `hatch-fancy-pypi-readme` plugin for build-time link transformation
  - Relative links (e.g., `docs/troubleshooting.md`) are converted to version-tagged GitHub URLs
  - PyPI users now see links pointing to the exact version they installed (e.g., `/blob/v0.1.3/docs/...`)
- **Development repository link** - Added prominent source link for PyPI users to find the GitHub repo

## [0.1.2] - 2026-01-10

### Added
- **Ruff linter/formatter** - Added to development workflow with pre-commit hooks and CI integration
- **Multi-version testing** - Docker-based test runner script for Python 3.10-3.14 (`/matrix` skill)
- **Artifact verification workflow** - New CI workflow runs 2 hours after nightly tests to verify generated artifacts

### Changed
- **Python version support** - Now supports Python 3.10-3.14 (dropped 3.9)
- **CI authentication** - Use `NOTEBOOKLM_AUTH_JSON` environment variable (inline JSON, no file writes)

### Fixed
- **E2E test cleanup** - Generation notebook fixture now only cleans artifacts once per session (was deleting artifacts between tests)
- **Nightly CI** - Fixed pytest marker from `-m e2e` to `-m "not variants"` (e2e marker didn't exist)
- macOS CI fix for Playwright version extraction (grep pattern anchoring)
- Python 3.10 test compatibility with mock.patch resolution

### Documentation
- Claude Code skill: parallel agent safety guidance
- Claude Code skill: timeout recommendations for all artifact types
- Claude Code skill: clarified `-n` vs `--notebook` flag availability

## [0.1.1] - 2026-01-08

### Added
- `NOTEBOOKLM_HOME` environment variable for custom storage location
- `NOTEBOOKLM_AUTH_JSON` environment variable for inline authentication (CI/CD friendly)
- Claude Code skill installation via `notebooklm skill install`

### Fixed
- Infographic generation parameter structure
- Mind map artifacts now persist as notes after generation
- Artifact export with proper ExportType enum handling
- Skill install path resolution for package data

### Documentation
- PyPI release checklist
- Streamlined README
- E2E test fixture documentation

## [0.1.0] - 2026-01-06

### Added
- Initial release of `notebooklm-py` - unofficial Python client for Google NotebookLM
- Full notebook CRUD operations (create, list, rename, delete)
- **Research polling CLI commands** for LLM agent workflows:
  - `notebooklm research status` - Check research progress (non-blocking)
  - `notebooklm research wait --import-all` - Wait for completion and import sources
  - `notebooklm source add-research --no-wait` - Start deep research without blocking
- **Multi-artifact downloads** with intelligent selection:
  - `download audio`, `download video`, `download infographic`, `download slide-deck`
  - Multiple artifact selection (--all flag)
  - Smart defaults and intelligent filtering (--latest, --earliest, --name, --artifact-id)
  - File/directory conflict handling (--force, --no-clobber, auto-rename)
  - Preview mode (--dry-run) and structured output (--json)
- Source management:
  - Add URL sources (with YouTube transcript support)
  - Add text sources
  - Add file sources (PDF, TXT, MD, DOCX) via native upload
  - Delete sources
  - Rename sources
- Studio artifact generation:
  - Audio overviews (podcasts) with 4 formats and 3 lengths
  - Video overviews with 9 visual styles
  - Quizzes and flashcards
  - Infographics, slide decks, and data tables
  - Study guides, briefing docs, and reports
- Query/chat interface with conversation history support
- Research agents (Fast and Deep modes)
- Artifact downloads (audio, video, infographics, slides)
- CLI with 27 commands
- Comprehensive documentation (API, RPC, examples)
- 96 unit tests (100% passing)
- E2E tests for all major features

### Fixed
- Audio overview instructions parameter now properly supported at RPC position [6][1][0]
- Quiz and flashcard distinction via title-based filtering
- Package renamed from `notebooklm-automation` to `notebooklm`
- CLI module renamed from `cli.py` to `notebooklm_cli.py`
- Removed orphaned `cli_query.py` file

### ⚠️ Beta Release Notice

This is the initial public release of `notebooklm-py`. While core functionality is tested and working, please note:

- **RPC Protocol Fragility**: This library uses undocumented Google APIs. Method IDs can change without notice, potentially breaking functionality. See [Troubleshooting](docs/troubleshooting.md) for debugging guidance.
- **Unofficial Status**: This is not affiliated with or endorsed by Google.
- **API Stability**: The Python API may change in future releases as we refine the interface.

### Known Issues

- **RPC method IDs may change**: Google can update their internal APIs at any time, breaking this library. Check the [RPC Development Guide](docs/rpc-development.md) for how to identify and update method IDs.
- **Rate limiting**: Heavy usage may trigger Google's rate limits. Add delays between bulk operations.
- **Authentication expiry**: CSRF tokens expire after some time. Re-run `notebooklm login` if you encounter auth errors.
- **Large file uploads**: Files over 50MB may fail or timeout. Split large documents if needed.

[Unreleased]: https://github.com/teng-lin/notebooklm-py/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/teng-lin/notebooklm-py/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/teng-lin/notebooklm-py/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/teng-lin/notebooklm-py/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/teng-lin/notebooklm-py/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/teng-lin/notebooklm-py/compare/v0.3.4...v0.4.0
[0.3.4]: https://github.com/teng-lin/notebooklm-py/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/teng-lin/notebooklm-py/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/teng-lin/notebooklm-py/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/teng-lin/notebooklm-py/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/teng-lin/notebooklm-py/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/teng-lin/notebooklm-py/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/teng-lin/notebooklm-py/compare/v0.1.4...v0.2.0
[0.1.4]: https://github.com/teng-lin/notebooklm-py/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/teng-lin/notebooklm-py/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/teng-lin/notebooklm-py/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/teng-lin/notebooklm-py/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/teng-lin/notebooklm-py/releases/tag/v0.1.0
