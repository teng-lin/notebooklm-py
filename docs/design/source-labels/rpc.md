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
#   [[ name_emoji, sources ]]
#      └ slot[3][0][0]   └ slot[3][0][1]
#
#   name_emoji = [name, emoji]   positional; None (or omit) = leave unchanged
#   sources    = [[source_id], ...]   each source UUID wrapped in a 1-element list;
#                                      absent = leave unchanged
```

### Variants

```python
# Rename (name_emoji = [name]; sources omitted)
params = [OPTS, notebook_id, label_id, [[[new_name]]]]

# Set emoji (name slot None, emoji set; sources omitted)
params = [OPTS, notebook_id, label_id, [[[None, emoji]]]]

# Add source(s) to the label (name_emoji = None, sources set)
params = [OPTS, notebook_id, label_id, [[None, [[source_id]]]]]
```

This is how a source is added to a label (the UI's source-row **"Move to"**
action). Set the `sources` group while leaving `name_emoji` as `None`.

> **`sources` is APPEND, not replace — confirmed (2026-06-06).** Adding one
> source to a label that already had 3 sent only the single new ID
> (`sources = [["<new>"]]`) and the label went from 3 → 4 sources — the existing
> members were preserved. So send **only the source(s) you want to add**, not the
> full list.

> **Labels may overlap — confirmed (2026-06-06).** The source added above
> remained in its original label as well, so it ended up in **two** labels at
> once. Only one `le8sX` (the append) fired; no removal occurred. The model is
> effectively **many-to-many** — a label owns a list of source IDs and nothing
> enforces a source belonging to a single label. (The source-row menu is labeled
> "Move to" but behaved as "add to".)

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
labels.add_sources(notebook_id, label_id, source_ids)        # -> le8sX (sources group; APPENDS)
labels.delete(notebook_id, label_ids)              # -> GyzE7e (accepts a list)
```

> `add_sources` appends — it does **not** remove the sources from any other label
> (sources may belong to multiple labels).

---

## Confirmed (2026-06-06)

- **`sources` group is append**, not replace (3 → 4 sources sending only the new ID).
- **Labels may overlap** — a source can be in multiple labels at once.
- **`I3xc3c` response nesting** is `[[label, ...]]`.

## Open items (not yet captured)

- **Removing a source from a label** (un-assign, without deleting the source).
  The source-row menu's "Remove source" **deletes the source from the notebook**,
  not from the label — no dedicated "remove from label" action was found. Since
  the `sources` group appends, removal likely needs a different flag/field or a
  separate RPC; unconfirmed.
