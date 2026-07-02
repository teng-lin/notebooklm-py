# ADR-0026: MCP Studio surface — notes + artifacts unified

## Status

Accepted.

## Context

NotebookLM's UI groups user notes and generated artifacts (audio, video, reports,
quizzes, mind maps, …) under one **Studio** panel. The MCP surface split them
across two domains: 8 `artifact_*` tools and 4 `note_*` tools (12 tools). This
diverged from the product's own mental model, and the split forced agents to know
*which* domain a given item lived in before acting — notably note-backed mind
maps, which are authored as notes but surface as artifacts.

Following ADR-0025 (discrete typed verbs, no polymorphic mega-tools) and the
#1731 tool-improvement program (35 tools), a review asked whether notes and
artifacts should merge into one "Studio" surface — and if so, how far, since a
rename alone nets only −1 tool.

## Decision

Adopt a **Studio surface** that renames the artifact tools and unifies the
cross-cutting *read/list/delete* operations across notes and artifacts, while
keeping note **authoring** distinct. Net **35 → 32 tools**.

1. **Rename** `artifact_*` → `studio_*` (8 tools). Internal `client.artifacts.*`,
   `ArtifactType`, and the `artifact`/`artifact_type`/`artifact_id` params are
   unchanged — MCP-adapter-only. The module file stays `tools/artifacts.py` (a
   rename would churn `server.py`/`_fileroutes.py` imports for no functional gain);
   cross-type plumbing lives in the sibling `tools/_studio.py` (extracted to stay
   under the ADR-0008 1000-line module cap).

2. **`note_save`** — an upsert folding `note_create` + `note_update` (−1). Mode is
   keyed solely on `note`: omitted → create (title+content required); given →
   update (unresolved ref → `NoteNotFoundError`, never a stray create). Precedent:
   `share_set_user` is an upsert.

3. **`studio_list(notebook, item?, kind?, limit?, offset?)`** — merges notes +
   artifacts into one `items` list with a shared **hyphenated** `type`
   (`note | audio | video | report | quiz | flashcards | mind-map | infographic |
   slide-deck | data-table`, plus pass-through `unknown`). Folds `note_list` (−1),
   including its by-ref single fetch via `item`.

4. **`studio_delete(notebook, item, confirm)`** — cross-type; folds `note_delete`
   (−1). Resolves `item` over the merged list and routes by resolved type:
   `note` → `DELETE_NOTE`, artifact → `DELETE_ARTIFACT` (which clears note-backed
   mind maps through the note system). Preview is unified as
   `action:"delete_studio_item"` with `item_id`/`type`/`title`.

`studio_rename` stays artifact-scoped: there was never a `note_rename` to fold
(notes rename via `note_save(note, title)`), so spanning it would be 0-count and
would route note renames through a title-only `execute_note_save` (`content=None`)
with no server-confirmed "None = leave unchanged" guarantee — a content-loss risk
the content-preserving CLI path (`execute_note_rename`) deliberately avoids.

## Consequences

- **Breaking** (experimental MCP surface): `note_create`/`note_update`/`note_list`/
  `note_delete` and `artifact_*` tool names are removed. Wire keys change:
  `studio_list` returns `items` (was `artifacts`/`notes`); `studio_delete` returns
  `item_id`/`type` (was `artifact_id`). Notes domain collapses to `note_save`.
- **No dedup pass is needed:** `client.notes.list()` drops `NoteRowKind.MIND_MAP`
  rows, so notes ∩ artifacts = ∅; the merge keeps a defensive dedup-by-id that
  never fires in practice.
- **Known inefficiency:** `studio_list` (and the `studio_delete` resolve step)
  issue `GET_NOTES_AND_MIND_MAPS` (`cFji9`) **twice** — once via `notes.list`, once
  via `artifacts.list`'s mind-map facade — plus `gArtLc` once. A single-fetch
  version needs an `_app` seam below both client APIs and is deferred as
  out-of-(MCP-adapter-only)-scope.
- **`studio_delete` stays idempotent-on-missing:** an absent *full UUID* routes to
  the artifact delete path (no raise), preserving delete-by-id idempotency; a
  non-UUID (prefix/title) miss is a real NOT_FOUND. Correct because a *present*
  note is always found in the merged list and routed to `DELETE_NOTE`.
- **Integration coverage:** cross-type `studio_delete` routing and the sharing
  domain are unit-covered but lack `mcp_vcr` cassettes (recording needs live auth);
  tracked in #1732 / #1733.
