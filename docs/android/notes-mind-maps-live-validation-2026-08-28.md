# Android note-backed mind-map live validation

**Date:** 2026-08-28
**Scope:** cross-backend classification and destructive semantics on disposable resources
**Retention:** sanitized semantic record; account, notebook, note, and credential values omitted

## Result

Two independent runs generated a note-backed mind map through the Web backend and then read the
same persisted row through Android `GetNotes`. The Web and Android ids matched exactly in each run.
Contrary to the earlier enum-only assumption, both Android rows carried:

- `ProjectNote.metadata.type = USER_WRITTEN (1)`;
- `ProjectNote.metadata.note_prompt_type = NOTE_PROMPT_TYPE_UNSPECIFIED (0)`; and
- a JSON object in `ProjectNote.content` with a top-level `children` key.

The second run reconfirmed the exact id, enum values, prompt value, and content classification before
cleanup. This proves that Web-generated maps cannot be classified from Android prompt metadata
alone. The evidence-safe classifier is the union already represented by the public Web contract:

```text
prompt type is MIND_MAP
OR parsed content is a JSON object containing top-level "children" or "nodes"
```

The same predicate must be negated for ordinary note listing so a Web-generated map is never
misreported as a text note.

## Deletion lifecycle

In the first run, one ordinary note and the generated map existed as siblings. One non-replayed
Android `DeleteNotes` request targeting only the exact map id removed the map. A bounded follow-up
`GetNotes` observed the map absent while the ordinary sibling remained. Repeating the public
delete attempt also succeeded against the already-absent id, and final disposable resource cleanup
succeeded. The adapter's preflight avoids sending that redundant destructive request.

The live result qualifies a kind-safe implementation:

1. read `GetNotes` and classify the requested id as a map;
2. return successfully without mutation when that map is absent (including when the id belongs to
   an ordinary note);
3. send `DeleteNotes` once with replay disabled;
4. treat a concurrent status-5 absence as idempotent success; and
5. poll bounded safe reads until the classified map id disappears, failing loud if it remains.

## Public raw compatibility boundary

The legacy `NotesAPI.list_mind_maps` contract returns raw Web rows. Android proves only the persisted
map id and content needed by existing leading-slot consumers, so its honest compatibility row is
exactly `[id, content]`. The adapter does not synthesize Web-only status, metadata, title, or creation
time. The private typed aggregate may additionally use the exact Android name and parsed tree, while
leaving `created_at=None` because the recovered timestamp is specifically a last-edit time.

## Ordinary-note soft-delete boundary (2026-08-29)

Two consecutive complete-manifest reruns created and updated an ordinary note through Android, then
deleted it through the non-replayed Android `DeleteNotes` path. Android bounded read-back reported the
id absent, and both Android and Web ordinary-note lists excluded it. Web exact-ID `get_or_none`,
however, exposed the persisted soft-delete row as `Note(id=<same id>, title="", content="")`, matching
Web's established raw `[id, None, 2]` tombstone behavior. Android's recovered `NoteOrStatus` note arm
did not provide a `ProjectNote` that could reproduce that exact result.

This is a third substitution blocker. The private Android adapter correctly treats the status-only or
absent Android projection as deleted and must not fabricate a Web tombstone `Note`. Cleanup deleted the
uniquely prefixed disposable notebooks, and each final prefix scan found no leaked resource.
