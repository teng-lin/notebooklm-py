# Upgrading to v0.8.0

**Status:** Active
**Last Updated:** 2026-06-03

`notebooklm-py` v0.8.0 lands a batch of **breaking** error-and-return contract
changes under [ADR-0019](adr/0019-error-and-return-contract.md) (umbrella
[#1346](https://github.com/teng-lin/notebooklm-py/issues/1346)). The guiding
principle: *a return value encodes only success and genuine async-lifecycle
state; resource absence, server refusal, and shape-drift **raise**.* `None`,
`""`, empty sentinels, `"not_found"` strings, and `ValueError` stop being used
to signal that an error happened.

This guide is the single consolidated reference for moving your code across the
0.7.0 → 0.8.0 boundary. **Nothing here is required to keep working on 0.7.0** —
every change below is still in its compatibility window. The goal is to let you
adopt the **forward-compatible** form *now*, so your code works on **both**
0.7.0 and 0.8.0 with no flag day.

For the canonical deprecation registry (with removal-version pins), see
[docs/deprecations.md](deprecations.md). For the design rationale, see
[ADR-0019](adr/0019-error-and-return-contract.md).

---

## Test your code against 0.8.0 early

> **Availability.** The **`NOTEBOOKLM_FUTURE_ERRORS`** preview flag ships as part
> of the v0.8.0 program (a dedicated PR under umbrella
> [#1346](https://github.com/teng-lin/notebooklm-py/issues/1346)). Once it lands
> in a 0.7.x patch, the workflow below works on the 0.7.x line; check the
> [CHANGELOG](../CHANGELOG.md) / [docs/configuration.md](configuration.md) for
> the exact patch that introduces it. On a 0.7.x build without the flag, setting
> it has no effect — fall back to the per-change `DeprecationWarning`s (the
> ✅ rows in the [summary table](#summary-table)) to drive your migration.

You don't have to wait for the 0.8.0 release to find out whether your code is
ready. Set **`NOTEBOOKLM_FUTURE_ERRORS=1`** and 0.7.x will **raise the v0.8.0
errors today** — `get()` raises `*NotFoundError`, synchronous generation refusals
raise instead of returning `status="failed"`, mutate-existing ops raise on a
missing target, and the ambiguous `research.poll` selection raises instead of
warning. Everything else stays on the 0.7.x code path.

```bash
# Run your test suite (or your app) against v0.8.0 behavior, on 0.7.0:
NOTEBOOKLM_FUTURE_ERRORS=1 pytest
NOTEBOOKLM_FUTURE_ERRORS=1 python my_app.py
```

```yaml
# Or wire it into CI so you catch a regression before upgrading:
env:
  NOTEBOOKLM_FUTURE_ERRORS: "1"
```

The flag is a **preview/opt-in** switch — it does not change the default 0.7.0
behavior for anyone who doesn't set it, and it goes away once 0.8.0 makes the new
behavior unconditional. Use it to drive your migration: turn it on, run your
tests, fix what breaks using the migrations below, and you're ready for 0.8.0.

> `NOTEBOOKLM_FUTURE_ERRORS` and `NOTEBOOKLM_QUIET_DEPRECATIONS` are
> complementary: the first opts you **into** future *raising* behavior to test
> it; the second silences the *warnings* the current behavior emits while you
> migrate. They can be set together.

> **Surfacing the warnings.** Python **hides `DeprecationWarning` by default**
> outside `__main__`, so the per-change runways may be invisible in a normal run.
> To see them, enable them explicitly:
> ```bash
> python -W error::DeprecationWarning -m pytest          # turn them into errors
> PYTHONWARNINGS=default::DeprecationWarning python app.py  # just print them
> ```
> But `NOTEBOOKLM_FUTURE_ERRORS=1` is the more reliable forward-test: it
> **raises** (not warns), so it surfaces regardless of your warning filters — and
> it is the **only** 0.7.0 signal for the silent behavioral breaks (return-value
> flips, refusal-raises, mutate-existing fail-loud) that emit no
> `DeprecationWarning` at all.

---

## Summary table

| Change | Warns today? | Migration (forward-compatible) | Breaks in |
|--------|--------------|--------------------------------|-----------|
| `sources` / `artifacts` / `notes` / `mind_maps` `.get()` returns `None` on a miss | ✅ on a miss | `get_or_none()`, or `try/except *NotFoundError` | v0.8.0 |
| Dict-subscript (`result["key"]`) on typed research / mind-map / source-guide returns | ✅ on subscript | Attribute access (`result.status`, `guide.summary`) | v0.8.0 |
| `research.wait_for_completion(interval=...)` | ✅ on use | `initial_interval=...` | v0.8.0 |
| `generate mind-map` default `--kind` flips note-backed → interactive | ✅ stderr notice | Pass `--kind note-backed` (or `--kind interactive`) explicitly | v0.8.0 |
| `NotebooksAPI.share()` removed | ✅ on use | `client.sharing.set_public(...)` | v0.8.0 |
| `research.poll`/`wait_for_completion` `task_id=None` with ≥2 in-flight tasks | ✅ on ambiguity | Pass `task_id=` from `research.start` | v0.8.0 |
| Synchronous generation refusal returns `GenerationStatus(status="failed")` | ❌ silent | `try/except RateLimitError` (or `with_rate_limit_retry`) | v0.8.0 |
| `notes.update` / `rename(return_object=False)` silently no-op on a missing target | ❌ silent | `try/except *NotFoundError` | v0.8.0 |
| `sources.refresh` / `chat.delete_conversation` return `bool` (always `True`) | ❌ silent | Stop relying on the return value | v0.8.0 |

Legend: ✅ emits a `DeprecationWarning` (or a stderr notice) in 0.7.0 you can see
today; ❌ is a **silent** clean break — `NOTEBOOKLM_FUTURE_ERRORS=1` is the only
way to surface it before upgrading.

---

## 1. `get()` raises `*NotFoundError` instead of returning `None`

**What changes.** `sources.get()`, `artifacts.get()`, `notes.get()`, and
`mind_maps.get()` raise the matching `*NotFoundError` on a miss instead of
returning `None`, unifying them with `notebooks.get()` (which already raises).

**Warning in 0.7.0** (fires only on a miss):

> `DeprecationWarning: sources.get() returning None on a miss is deprecated and
> will raise SourceNotFoundError in v0.8.0; use get_or_none() for None-on-miss.`

**Migration.** Two forward-compatible paths, both valid on 0.7.0 **and** 0.8.0:

```python
# BEFORE — relies on the None-on-miss return (warns on a miss in 0.7.0; raises in 0.8.0)
src = await client.sources.get(nb_id, source_id)
if src is None:
    ...  # not found

# AFTER (a) — want None on a miss, warning-free: use get_or_none()
src = await client.sources.get_or_none(nb_id, source_id)
if src is None:
    ...  # not found

# AFTER (b) — want the raising contract: catch the typed exception
from notebooklm import SourceNotFoundError
try:
    src = await client.sources.get(nb_id, source_id)
except SourceNotFoundError:
    ...  # not found
```

`get_or_none()` exists on all four namespaces today (`sources`, `artifacts`,
`notes`, `mind_maps`) and never warns. The `*NotFoundError` classes
(`SourceNotFoundError`, `ArtifactNotFoundError`, `NoteNotFoundError`,
`MindMapNotFoundError`) all exist today too — so the `try/except` form can be
written now; it only starts *raising* in v0.8.0. All four are exported from
both `notebooklm` and `notebooklm.exceptions`.

**Breaks in:** v0.8.0.
Tracked by [#1247](https://github.com/teng-lin/notebooklm-py/issues/1247).

---

## 2. Typed returns drop dict-subscript access

**What changes.** `research.poll` / `research.start` /
`research.wait_for_completion`, `artifacts.generate_mind_map`, and
`sources.get_guide` return typed dataclasses (`ResearchTask`, `ResearchStart`,
`MindMapResult`, `SourceGuide`). In 0.7.0 these still support legacy
dict-subscript access (`result["key"]`) via a back-compat mixin; v0.8.0 drops
the mixin and the returns become **attribute-only**.

**Warning in 0.7.0** (fires only on a `result["key"]` subscript):

> `DeprecationWarning: dict-style access on ResearchTask is deprecated; use
> attribute access (e.g. result.status). Subscript access is removed in v0.8.0.`

Note that `result.get(...)`, `result.keys()`, `"x" in result`, and
`iter(result)` stay **silent** in 0.7.0 — only `result["key"]` warns — but all
of the mapping interface is removed in v0.8.0.

**Migration.** Switch subscripts to attribute access (works on both releases):

```python
from notebooklm import ResearchStatus

# BEFORE — dict-subscript (warns in 0.7.0; removed in 0.8.0)
result = await client.research.poll(nb_id)
if result["status"] == "completed":
    for source in result["sources"]:
        print(source["title"], source["url"])

guide = await client.sources.get_guide(nb_id, src_id)
print(guide["summary"], guide["keywords"])

# AFTER — typed attribute access (warning-free, valid on both releases)
result = await client.research.poll(nb_id)
if result.status == ResearchStatus.COMPLETED:   # also == "completed"
    for source in result.sources:               # tuple[ResearchSource, ...]
        print(source.title, source.url)

guide = await client.sources.get_guide(nb_id, src_id)
print(guide.summary, guide.keywords)            # guide.keywords is a tuple
```

`ResearchStatus` is a `str` enum, so `result.status == "completed"` keeps
working. The attributes map one-to-one onto the old keys: `ResearchTask` has
`.task_id` / `.status` / `.query` / `.sources` / `.summary` / `.report` /
`.tasks` (the sibling-tasks tuple from a top-level poll, formerly
`result["tasks"]`); `ResearchSource` has `.title` / `.url`; `ResearchStart` has
`.task_id`;
`MindMapResult` has `.mind_map` / `.note_id`; `SourceGuide` has `.summary` /
`.keywords`. The types are exported from both `notebooklm` and
`notebooklm.types`.

**Breaks in:** v0.8.0.
Tracked by [#1251](https://github.com/teng-lin/notebooklm-py/issues/1251)
(follow-up to [#1209](https://github.com/teng-lin/notebooklm-py/issues/1209)).

---

## 3. `research.wait_for_completion(interval=...)` → `initial_interval=...`

**What changes.** The deprecated `interval=` keyword alias on
`ResearchAPI.wait_for_completion` is removed; only `initial_interval=` is
accepted. The rename aligns it with `SourcesAPI.wait_until_ready` and
`ArtifactsAPI.wait_for_completion`, which already spell the cadence
`initial_interval`.

**Warning in 0.7.0** (fires only when you pass a non-default `interval=`):

> `DeprecationWarning: the 'interval' keyword is deprecated; use
> 'initial_interval' instead. 'interval' is removed in v0.8.0.`

Passing **both** `interval` and `initial_interval` raises `TypeError` today.

**Migration.** Rename the keyword (same cadence, valid on both releases):

```python
# BEFORE — deprecated alias (warns in 0.7.0; removed in 0.8.0)
await client.research.wait_for_completion(nb_id, task_id, interval=2.0)

# AFTER — canonical keyword, matches the source/artifact waiters
await client.research.wait_for_completion(nb_id, task_id, initial_interval=2.0)
```

**Breaks in:** v0.8.0.
Tracked by [#1254](https://github.com/teng-lin/notebooklm-py/issues/1254)
(follow-up to [#1208](https://github.com/teng-lin/notebooklm-py/issues/1208)).

---

## 4. `generate mind-map` default `--kind` flips to interactive

**What changes (CLI).** `notebooklm generate mind-map` defaults to the
`note-backed` kind today; in v0.8.0 the default flips to `interactive` (matching
what NotebookLM's web app now creates). Note-backed stays available via an
explicit `--kind note-backed` — it is **not** deprecated.

**Notice in 0.7.0** (printed to stderr when you don't pass `--kind`):

> `Note: 'generate mind-map' defaults to the note-backed kind today, but the
> default switches to interactive in v0.8.0 (NotebookLM's web app already creates
> interactive maps). Pass --kind note-backed or --kind interactive to pin your
> choice; set NOTEBOOKLM_QUIET_DEPRECATIONS=1 to silence.`

**Migration.** Pin the kind explicitly so the default flip can't change your
output shape (works identically on both releases):

```bash
# BEFORE — relies on the default (note-backed today, interactive in v0.8.0)
notebooklm generate mind-map <notebook-id>

# AFTER — pin your choice; behavior is stable across the version boundary
notebooklm generate mind-map <notebook-id> --kind note-backed   # JSON tree, synchronous
notebooklm generate mind-map <notebook-id> --kind interactive   # studio artifact, polled
```

> **Python API note.** This is a **CLI-only** default change. The Python methods
> are already kind-specific and unaffected:
> `client.artifacts.generate_mind_map(...)` returns the interactive
> `MindMapResult`, and the note-backed path has its own methods. There is no
> default to flip on the API.

**Breaks in:** v0.8.0.
Tracked by [#1272](https://github.com/teng-lin/notebooklm-py/issues/1272).

---

## 5. `NotebooksAPI.share()` is removed

**What changes.** The deprecated `client.notebooks.share()` wrapper (a
no-behavior-change shim, deprecated since v0.5.0) is removed.

**Warning in 0.7.0** (fires on every call):

> `DeprecationWarning: NotebooksAPI.share() is deprecated; use
> client.sharing.set_public() (with add_user() / set_view_level() /
> get_status()). Removed in v0.8.0.`

**Migration.** Call the `sharing` namespace directly (available on both
releases):

```python
# BEFORE — deprecated wrapper (warns in 0.7.0; removed in 0.8.0)
await client.notebooks.share(nb_id, public=True)

# AFTER — the sharing namespace
await client.sharing.set_public(nb_id, public=True)

# For the artifact deep-link URL (previously folded into share()):
url = client.notebooks.get_share_url(nb_id, artifact_id)
```

`set_public()` pairs with `add_user()`, `set_view_level()`, and `get_status()`
for the full sharing surface; `get_share_url()` already exists today.

**Breaks in:** v0.8.0.
Tracked by [#1363](https://github.com/teng-lin/notebooklm-py/issues/1363).

---

## 6. Ambiguous `research.poll` / `wait_for_completion` selection raises

**What changes.** `research.poll(nb_id)` and
`research.wait_for_completion(nb_id)` keep `task_id` **optional**. With exactly
one task in flight, `task_id=None` still returns it (no change). But with **two
or more** tasks in flight and no `task_id`, v0.8.0 **raises** (a typed research
error) instead of silently returning the *latest* task. This kills the
silent-wrong-task hazard while keeping the single-task convenience.

**Warning in 0.7.0** (fires only when ≥2 tasks are in flight and no `task_id` is
passed):

> `DeprecationWarning: research.poll() with multiple tasks in flight and no
> task_id returns the latest task; pass task_id (from research.start) to select
> one. This becomes an error in a future release.`

**Migration.** Capture the `task_id` from `research.start` and pass it through
(works on both releases — single-task callers need no change):

```python
# BEFORE — implicit "latest task" selection (warns with ≥2 in flight; raises in 0.8.0)
await client.research.start(nb_id, query)
result = await client.research.poll(nb_id)

# AFTER — pass the discriminator from start()
started = await client.research.start(nb_id, query)
if started is not None:                            # start() returns ResearchStart | None
    result = await client.research.poll(nb_id, task_id=started.task_id)
    # ...and likewise:
    result = await client.research.wait_for_completion(nb_id, task_id=started.task_id)
```

**Breaks in:** v0.8.0.
Tracked by [#1363](https://github.com/teng-lin/notebooklm-py/issues/1363).

---

## 7. Synchronous generation refusal raises (no more `status="failed"`)

**What changes.** When a generation kickoff
(`generate_audio` / `generate_video` / `generate_mind_map` / `revise_slide` /
…) is **refused synchronously** by the server (e.g. a rate limit /
`USER_DISPLAYABLE_ERROR`), 0.7.0 *swallows* it into
`GenerationStatus(status="failed")`. Per ADR-0019, a refusal is *couldn't-start*
and v0.8.0 **raises** the `RateLimitError` / `RPCError` the decoder already
produces. (A poll that observes a *started-then-failed* task still returns a
terminal `failed` status — that is real async data, unchanged.)

**Warning in 0.7.0:** **None — this is a silent clean break.** There is no
`DeprecationWarning`; the only way to surface it before upgrading is
`NOTEBOOKLM_FUTURE_ERRORS=1`.

**Migration.** Catch `RateLimitError` (and/or `RPCError`) around the kickoff
instead of inspecting a returned `status`. This pattern is valid on both
releases — on 0.7.0 the refusal is still returned as `status="failed"`, so keep
the status check too if you support both during the transition:

```python
from notebooklm import RateLimitError

# BEFORE — inspects the returned status (only catches the swallowed refusal on 0.7.0)
status = await client.artifacts.generate_audio(nb_id)
if status.is_failed:
    ...  # may be a refusal OR a started-then-failed task — ambiguous

# AFTER — catch the refusal; a returned failed status is genuine async failure
try:
    status = await client.artifacts.generate_audio(nb_id)
except RateLimitError:
    ...  # synchronous refusal (couldn't start) — retry later / back off
else:
    if status.is_failed:
        ...  # the task started and then reached a terminal failed state
```

If you use the built-in retry helper, prefer it — it is rewritten for this
contract and handles both the 0.7.0 returned-status and the 0.8.0 raised-error
shapes:

```python
from notebooklm.artifacts import with_rate_limit_retry

status = await with_rate_limit_retry(
    lambda: client.artifacts.generate_audio(nb_id),
    max_retries=3,
)
```

**Breaks in:** v0.8.0.
Tracked by [#1342](https://github.com/teng-lin/notebooklm-py/issues/1342).

---

## 8. `notes.update` / `rename(return_object=False)` fail loud on a missing target

**What changes.** Mutate-existing operations against a **missing** target start
raising `*NotFoundError` instead of silently no-op'ing:

- `notes.update(...)` on a non-existent note raises `NoteNotFoundError` (today it
  silently "succeeds").
- `sources.rename(..., return_object=False)` and
  `artifacts.rename(..., return_object=False)` raise `SourceNotFoundError` /
  `ArtifactNotFoundError` on a missing target. The `return_object` flag now
  controls **hydration/return only**, not miss-detection — both modes raise on a
  missing target. (`return_object=True` already fails loud on 0.7.0;
  `mind_maps.rename` already fails loud in both modes.)

**Warning in 0.7.0:** **None — this is a silent clean break.** Surface it with
`NOTEBOOKLM_FUTURE_ERRORS=1`.

**Migration.** Wrap the mutation in `try/except *NotFoundError`. The classes
exist today, so this is forward-compatible — on 0.7.0 the missing-target case is
still a silent no-op (the `except` simply never fires); on 0.8.0 it catches the
raise:

```python
from notebooklm import NoteNotFoundError, SourceNotFoundError

# notes.update
try:
    await client.notes.update(nb_id, note_id, "new content", "new title")
except NoteNotFoundError:
    ...  # the note doesn't exist (silently no-op on 0.7.0; raises on 0.8.0)

# rename(return_object=False)
try:
    await client.sources.rename(nb_id, source_id, "new title", return_object=False)
except SourceNotFoundError:
    ...  # the source doesn't exist
```

**Breaks in:** v0.8.0.
Tracked by [#1362](https://github.com/teng-lin/notebooklm-py/issues/1362).

---

## 9. `sources.refresh` / `chat.delete_conversation` return `None`

**What changes.** `SourcesAPI.refresh` and `ChatAPI.delete_conversation` are
annotated `-> bool` but only ever return `True` (any failure raises first). To
remove the misleading signal — mirroring the `delete() -> None` cleanup already
shipped on the 0.7.0 breaking line — they become `-> None` in v0.8.0.

> **No clean forward-compatible single-line form.** On 0.7.0 these return a
> truthy `True`; on 0.8.0 they return `None` (falsy). Any code shaped like
> `if await client.sources.refresh(...):` flips behavior across the boundary.
> The fix is to **stop branching on the return value** — success is signalled by
> *not raising*, not by the return — which is correct on both releases.

**Warning in 0.7.0:** **None — this is a silent clean break.** It is a
return-type change with no runtime warning. (`NOTEBOOKLM_FUTURE_ERRORS=1` does
**not** surface this one: it changes *error-raising* behavior, and this is a
benign return-value change, not an error. Audit for it by grep.)

**Migration.** Drop the truthiness check; rely on the call not raising:

```python
# BEFORE — branches on the always-True return (flips on 0.8.0, where it returns None)
if await client.sources.refresh(nb_id, source_id):
    print("refreshed")

# AFTER — success == no exception; works on both releases
await client.sources.refresh(nb_id, source_id)
print("refreshed")

# Same shape for chat:
await client.chat.delete_conversation(nb_id, conversation_id)
```

> `ChatAPI.clear_cache()` is **not** changing — its `bool` return is meaningful
> (it reports whether anything was cleared) and stays `-> bool`.

**Breaks in:** v0.8.0.
Tracked by [#1290](https://github.com/teng-lin/notebooklm-py/issues/1290).

---

## See also

- [docs/deprecations.md](deprecations.md) — the single source of truth for
  deprecated APIs with removal-version pins.
- [docs/adr/0019-error-and-return-contract.md](adr/0019-error-and-return-contract.md)
  — the design rationale for the whole convergence.
- [docs/stability.md](stability.md) — the semver promise and 0.x pre-1.0
  deprecation policy.
- [docs/configuration.md](configuration.md) — `NOTEBOOKLM_QUIET_DEPRECATIONS`
  and other environment variables.
