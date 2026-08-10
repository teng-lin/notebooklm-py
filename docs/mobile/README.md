# Mobile API reverse-engineering

`notebooklm-py` drives NotebookLM's **web** `batchexecute` transport. The official
**Android** app drives the *same backend services* over gRPC — where fields are
tag-addressed instead of positional. That makes the mobile surface a useful oracle
for the web one, and these files are what came out of reading it.

## Why this exists

Web `batchexecute` responses are positional JSON arrays. Nothing on the wire says
what index `3` means, so a wrong index is silent — it yields a plausible value of
the wrong thing. The mobile app's protobuf schema names every field, and the two
line up exactly:

```
JSON index i  ==  protobuf tag (i + 1)
```

That equivalence is what lets [`tests/_guardrails/test_wire_contract.py`](../../tests/_guardrails/test_wire_contract.py)
check our hardcoded positional constants against a real schema instead of against
themselves.

## What's here

| file | kind | notes |
|---|---|---|
| [`schema.proto`](schema.proto) | **generated** | 282 messages / 767 fields recovered from the Dart AOT `BuilderInfo`. **Parsed by CI** — see caveats. |
| [`enums.txt`](enums.txt) | **generated** | 77 enums / ~1900 values with exact integers. **Parsed by CI.** |
| [`endpoints.md`](endpoints.md) | reference | The gRPC method surface, and the mobile ⇄ web cross-reference. Start here. |
| [`capture.md`](capture.md) | runbook | How to intercept the app's HTTP/2 gRPC traffic (emulator, VPN, Mockttp). |
| [`blutter-dart3.13.patch`](blutter-dart3.13.patch) | tooling | Port of [blutter](https://github.com/worawit/blutter) to Dart 3.13, needed to decompile this app's snapshot. |

`schema.proto` and `enums.txt` are **regenerable artifacts, not hand-written docs**.
They are committed because CI parses them; regenerate rather than hand-edit.
The generator is [`scripts/parse_pbschema.py`](../../scripts/parse_pbschema.py).

## Caveats that will bite you

**`fieldType` in `schema.proto` is a parse failure, not a field name.** The
extractor emits that placeholder where it could not recover a real name — 11 of
767 fields. Do not treat it as real.

**Several messages appear twice with *different* tags.** One copy is the wire
schema (`…orchestration.v1`, `…tailwind.v1`), the other is the app's local
persistence schema (`…mobile.app.protos.persistence`). Always scope a lookup to
the right package; the guardrail refuses ambiguous matches rather than guessing.

**Use the merged enum dump, not the object pool alone.** The snapshot object pool
yields 74 enums / 273 values; merging it with the object store yields 77 / ~1900.
Auditing against the pool alone manufactures false "we invented this value"
findings and hides real members — `ARTIFACT_PENDING_REVIEW` was missed exactly
that way.

**`addUnused()` means the *client* ignores a field, not that the backend omits it.**
Roughly half of the `addUnused()` slots in the messages this client touches are
populated on the wire. "Mobile doesn't model it" is not evidence of absence.

**`addUnused()` reserves a field *slot*, not a tag *number*.** The reserved slots
take the next real tags, which are not consecutive — `ProjectMetadata` runs
`userRole`=1, five unused, `createTime`=**9**. Counting gives you *how many* tags
live in a gap, not *which*.

## Reproducing the recovery

The APK itself is **not** committed (~39 MB of proprietary binaries, gitignored).
Fetch your own copy, then follow [`capture.md`](capture.md) for traffic capture and
apply [`blutter-dart3.13.patch`](blutter-dart3.13.patch) for snapshot decompilation.
Both record the exact app build they were verified against.
