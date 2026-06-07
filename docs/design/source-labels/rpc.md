# Source Labels (Auto-label by Topic)

**Status:** Proposed / reverse-engineered (not yet implemented in `src/notebooklm/`)
**Last Updated:** 2026-06-06
**Source of Truth:** Live traffic capture (Chrome DevTools Protocol) against
`https://notebooklm.google.com/notebook/c3f6285f-...` on 2026-06-06.
**Purpose:** Document the RPCs behind NotebookLM's "Auto-label sources by topic"
feature so it can be added to the client.

> All payloads below are the **decoded `f.req` inner arrays** exactly as observed
> on the wire, rendered in Python (`null` → `None`). Each labeling RPC begins with
> the same request-options wrapper as other write RPCs in this client; see
> [Request-options wrapper](#request-options-wrapper).

---

## Overview

"Auto-label sources by topic" groups a notebook's sources into AI-generated
topic **labels**. A label is a standalone entity, **not** a field on a source —
a source carries no back-reference to its label; the label owns a list of source
IDs. Membership is therefore **many-to-many**: a source can appear under more
than one label at once (confirmed empirically — see
[UPDATE_LABEL](#rpc-update_label-le8sx)).

The UI control lives in the source panel, just above the source list:

- **Auto-label your sources by topic** button (`aria-label="Auto-label your sources by topic"`,
  becomes `aria-label="Undo or re-label sources"` once labels exist). Opens a menu:
  - **Add new label** — create an empty, manually-named label
  - **Reorganize** → **All sources** | **Unlabeled sources**
  - **Return to list view**
- Each label group has a `more_vert` menu: **Rename**, **Remove**, **Add emoji**.

---

## RPC Quick Reference

| RPC ID | Suggested Method | Purpose |
|--------|------------------|---------|
| `agX4Bc` | CREATE_LABEL | Auto-generate label groupings **and** create manual labels |
| `I3xc3c` | LIST_LABELS | List existing labels for a notebook |
| `le8sX` | UPDATE_LABEL | Rename a label, set its emoji, and/or add sources |
| `GyzE7e` | DELETE_LABEL | Delete one or more labels (batch) |

All endpoints: `POST /_/LabsTailwindUi/data/batchexecute?rpcids=<id>&source-path=/notebook/<notebook_id>&...`

---

## Request-options wrapper

Every labeling RPC's first argument is the recurring request-options structure
used elsewhere in this client (cf. `_settings.py`):

```python
OPTS = [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]]
```

The `[1, None, None, None, None, None, None, None, None, None, [1]]` sub-array is
the same "context/capabilities" block seen in `build_get_user_settings_params()`.

---

## The Label Tuple (response shape)

`agX4Bc` and `I3xc3c` both return the **full** label set after the operation:

```python
[
    None,
    [
        label,   # see below
        ...
    ],
]
```

Each `label` is a 4-tuple:

| Slot | Field | Notes |
|------|-------|-------|
| `[0]` | `name` | str |
| `[1]` | `sources` | `[[source_id], ...]` when populated; **`None`** for a new empty label. **Each source UUID is wrapped in its own single-element list** (`[source_id]`), so slot `[1]` is a list of those one-element lists. |
| `[2]` | `label_id` | server-assigned UUID |
| `[3]` | `emoji` | `""` when unset, else the emoji string |

Example (a freshly-created empty label alongside two populated ones):

```python
[None, [
    ["New Label",              None,                                  "b469c51d-7f8f-414a-9707-d2056230fee6", ""],
    ["VCR Testing",            [["1fcb3727-..."], ["667abb4e-..."], ["d1037c23-..."]], "b9ca0355-83cc-4c98-b9f4-9f93ebe40b97", ""],
    ["TypeScript Programming", [["ddd31154-..."], ["fdfc8ac4-..."]],  "f6a7b107-156b-4c04-ba0c-49dc0cdb7fc5", ""],
]]
```

---

## RPC: CREATE_LABEL (agX4Bc)

A single multi-mode RPC. The mode is selected by which slot is populated:

- slot `[4]` — **auto-label scope** (AI generates groupings)
- slot `[5]` — **manual labels to create** (no AI)

```python
# Full signature
params = [
    OPTS,            # [0] request options
    notebook_id,     # [1] notebook UUID (str)
    None,            # [2]
    None,            # [3]
    auto_scope,      # [4] auto-label scope, or None
    manual_labels,   # [5] manual labels to create (omit/absent for auto modes)
]
```

### Mode 1 — Auto-label / Reorganize → All sources

Re-labels **every** source from scratch. Existing labels are wiped and
regenerated with **new** label IDs.

```python
params = [OPTS, notebook_id, None, None, []]   # slot [4] = []
```

### Mode 2 — Reorganize → Unlabeled sources

Labels **only** currently-unlabeled sources; existing labels are preserved.

```python
params = [OPTS, notebook_id, None, None, [0]]  # slot [4] = [0]
```

> The "Unlabeled sources" menu item only appears in the UI when unlabeled
> sources actually exist.

### Mode 3 — Add new (manual) label

Creates one or more empty, named labels (no AI grouping). Slot `[4]` is `None`,
slot `[5]` holds `[[name, emoji], ...]`.

```python
params = [OPTS, notebook_id, None, None, None, [["New Label", ""]]]  # slot [5]
```

The created label is returned with `sources = None` (see [the label tuple](#the-label-tuple-response-shape))
and a server-assigned `label_id`.

**Response (all modes):** the full label set — `[None, [label, ...]]`.

---

## RPC: LIST_LABELS (I3xc3c)

Called on notebook load to fetch existing labels.

```python
params = [OPTS, notebook_id]
```

**Response:** `[[label, ...]]` — a single-element outer list wrapping the list of
labels (confirmed 2026-06-06; **not** `[None, [label, ...]]` like `agX4Bc`). The
`label` 4-tuple is the same as above.

**Listing labels returns the full label→source membership.** Each label's slot
`[1]` contains the UUIDs of its sources, with **each source UUID wrapped in its
own single-element list**:

```python
label[1] = [[source_uuid], [source_uuid], ...]   # one nested list per source
```

These are the same source UUIDs used by the source RPCs (`tGMBJ`, `b7Wfje`,
etc.), so a single `list()` call gives the complete source→label mapping with no
cross-referencing required. A brand-new empty label has `label[1] = None` instead
of a list.

---

## RPC: UPDATE_LABEL (le8sX)

A single label-update RPC covering **rename, emoji, and source membership**. The
4th argument (`slot[3]`) is a unified fieldmask; populate only the field group(s)
you want to change.

```python
params = [OPTS, notebook_id, label_id, fieldmask]

# fieldmask shape:
#   [[ name_emoji, sources_add, sources_remove ]]
#      └ slot[3][0][0]   └ slot[3][0][1]   └ slot[3][0][2]
#
#   name_emoji     = [name, emoji]    positional; None (or omit) = leave unchanged
#   sources_add    = [[source_id]]    source UUID wrapped in a 1-element list; absent = no add
#   sources_remove = [[source_id]]    source UUID wrapped in a 1-element list; absent = no remove
#
# IMPORTANT: each call processes only ONE source — the server honours just the
# FIRST entry of sources_add and the FIRST entry of sources_remove. To touch N
# sources, make N calls. (Confirmed 2026-06-07; see "one source per call" below.)
```

### Variants

```python
# Rename (name_emoji = [name]; sources omitted)
params = [OPTS, notebook_id, label_id, [[[new_name]]]]

# Set emoji (name slot None, emoji set; sources omitted)
params = [OPTS, notebook_id, label_id, [[[None, emoji]]]]

# Add ONE source to the label (name_emoji = None, sources_add set)
params = [OPTS, notebook_id, label_id, [[None, [[source_id]]]]]

# Remove ONE source from the label (name_emoji = None, sources_add = None, sources_remove set)
params = [OPTS, notebook_id, label_id, [[None, None, [[source_id]]]]]
```

Adding a source is the UI's source-row **"Move to"** action. **Removing** a
source from a label has **no UI control** (the source-row "Remove source"
deletes the source from the *notebook*), but the RPC supports it via the third
fieldmask slot — confirmed empirically (2026-06-07, see below).

> **`sources_add` is APPEND, not replace — confirmed (2026-06-06).** Adding one
> source to a label that already had 3 sent only the single new ID
> (`sources_add = [["<new>"]]`) and the label went from 3 → 4 sources — the
> existing members were preserved. So send **only the source you want to add**.

> **Source removal works via `sources_remove` (slot `[3][0][2]`) — confirmed
> (2026-06-07).** Sending `[[None, None, [[source_id]]]]` against a label holding
> `{A, B, C}` removed exactly that source, leaving `{A, B}`. The removed source
> **still exists in the notebook** (it is un-assigned, not deleted), and removal
> is **isolated to the target label** — a source that also belongs to another
> label stays in that other label (overlap preserved). Removing a non-member is a
> silent no-op (`[]`, no error). Removing the last member leaves the label present
> but empty. The web UI simply never sends this slot.

> **One source per call — confirmed (2026-06-07).** `le8sX` honours only the
> **first** entry of `sources_add` and the **first** of `sources_remove`. Sending
> `sources_add = [[A], [B], [C]]` added only `A`; sending `sources_remove =
> [[A], [B]]` removed only `A`. To add/remove N sources, issue N separate calls.
> A combined add+remove in one call is also unreliable — `[[None, [[C]], [[A]]]]`
> against `{A, B}` removed `A` but did **not** add `C` (ended `{B}`); keep adds
> and removes in separate calls.

> **Labels may overlap — confirmed (2026-06-06).** A source added to a second
> label remained in its original label too, so it ended up in **two** labels at
> once. The model is effectively **many-to-many** — a label owns a list of source
> IDs and nothing enforces a source belonging to a single label. (The source-row
> menu is labeled "Move to" but behaves as "add to".)

**Response:** `[]` on success.

---

## RPC: DELETE_LABEL (GyzE7e)

Deletes one or more labels. The label IDs are passed as an **array**, so this is
batch-capable. Deleting a label does **not** delete its sources — they become
unlabeled.

```python
params = [OPTS, notebook_id, [label_id, ...]]
```

**Response:** `[]` on success.

---

## Write-path requirements

Observed from a live `curl` of `agX4Bc`:

1. **`at` token is required** for all mutating calls (`agX4Bc`, `le8sX`,
   `GyzE7e`). It is sent in the POST body as `&at=<token>:<timestamp>&`
   (the XSRF token, same as other write RPCs in this client). Reads (`I3xc3c`)
   do not require it.
2. **Query params:** `rpcids`, `source-path=/notebook/<id>`, `bl` (build label),
   `f.sid` (session id), `_reqid`, `rt=c`, `hl`. `bl`/`f.sid` are scraped from
   the bootstrap page WIZ data, as the existing transport already does.

---

## Suggested client surface

```python
# Generate (AI grouping — the UI's "Auto-label" first run AND "Reorganize" re-run)
labels.generate(notebook_id, scope="all")        # -> agX4Bc, slot[4]=[]
labels.generate(notebook_id, scope="unlabeled")  # -> agX4Bc, slot[4]=[0]

# Manual
labels.create(notebook_id, name, emoji="")         # -> agX4Bc, slot[5]=[[name, emoji]]

# Read
labels.list(notebook_id)                           # -> I3xc3c

# Mutate (all via le8sX; set only the fields you want to change)
labels.update(notebook_id, label_id, name=None, emoji=None)  # -> le8sX (name_emoji group)
labels.add_sources(notebook_id, label_id, source_ids)        # -> le8sX (sources_add; one call PER source)
labels.remove_sources(notebook_id, label_id, source_ids)     # -> le8sX (sources_remove; one call PER source)
labels.delete(notebook_id, label_ids)              # -> GyzE7e (accepts a list)
```

> Both `add_sources` and `remove_sources` must issue **one `le8sX` call per
> source** (the server honours only the first id per call — see "one source per
> call"). `add_sources` appends and `remove_sources` un-assigns; neither touches
> any *other* label's membership (sources may belong to multiple labels).

---

## Confirmed (2026-06-06)

- **`sources_add` group is append**, not replace (3 → 4 sources sending only the new ID).
- **Labels may overlap** — a source can be in multiple labels at once.
- **`I3xc3c` response nesting** is `[[label, ...]]`.

## Confirmed (2026-06-07)

Verified against a live throwaway notebook with raw `le8sX` payloads:

- **Source removal exists** via the third fieldmask slot
  `slot[3][0][2]` (`sources_remove`): `[[None, None, [[source_id]]]]`. The web UI
  has no control for it, but the RPC honours it.
- **Removal un-assigns, it does not delete** — the removed source still exists in
  the notebook's source list.
- **Removal is label-scoped** — a source in two labels, removed from one, remains
  in the other (overlap preserved).
- **Removing a non-member** is a silent no-op (`[]`, no error); **removing the
  last member** leaves the label present but empty.
- **One source per call** — `le8sX` honours only the first entry of `sources_add`
  and the first of `sources_remove`. Multi-element lists silently drop all but the
  first. A combined add+remove in one call dropped the add. Do one mutation per call.

> ⚠️ **Client-code implication:** the current `add_sources` /
> `build_update_label_params` pack a multi-id add into a single `le8sX` call
> (`sources_add = [[a], [b], [c]]`), which the server truncates to the first id.
> Multi-id `client.labels.add_sources(nb, lbl, [a, b, c])` therefore only adds
> `a`. A correct implementation must loop one call per source (and the same holds
> for any future `remove_sources`).

## Open items (not yet captured)

- None outstanding for the membership lifecycle. Combined add+remove ordering /
  precedence within one call is only partially characterised (add was dropped when
  both groups were set); not pursued further since one-mutation-per-call is the
  reliable contract.
