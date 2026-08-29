# Android API reverse-engineering

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
| [`proto-evidence-ledger.md`](proto-evidence-ledger.md) | admission ledger | Exact/local cumulative compile closure, replay policy, and evidence-gated omissions for implemented Android B1-B6 adapters. |
| [`grpc-service-signature-exceptions.json`](grpc-service-signature-exceptions.json) | machine-readable admission manifest | Implemented full paths omitted from the exact generated service because a remote request or response protobuf FQN remains unproven. |
| [`capture.md`](capture.md) | runbook | How to intercept the app's HTTP/2 gRPC traffic (emulator, VPN, Mockttp). |
| [`android-traffic-capture.md`](android-traffic-capture.md) | legacy runbook | Rooted-emulator Cronet/Frida capture procedure retained as dated evidence. |
| [`auth-research.md`](auth-research.md) | live report | Exact NotebookLM Android OAuth identity, scope bundle, and bearer validation. |
| [`file-transfer-live-validation-2026-08-27.md`](file-transfer-live-validation-2026-08-27.md) | live report | Android file upload and artifact download protocol, failures, and successful replay. |
| [`deep-research-mobile-grpc-2026-08-27.md`](deep-research-mobile-grpc-2026-08-27.md) | live report | APK-absent Deep Research methods routed by the mobile backend, wire contract, reproducer, and interception. |
| [`labels-collections-copy-mobile-grpc-2026-08-27.md`](labels-collections-copy-mobile-grpc-2026-08-27.md) | live report | Full label/collection CRUD and memberships plus notebook copy through the mobile backend. |
| [`web-parity-gap-live-validation-2026-08-27.md`](web-parity-gap-live-validation-2026-08-27.md) | live report | Complete APK-vs-web gap routing audit, rich-copy fidelity, source/report probes, and destructive validation on the copy. |
| [`notebooks-live-validation-2026-08-28.md`](notebooks-live-validation-2026-08-28.md) | live report | Sanitized Android notebook emoji set/clear/combined read-back plus the repeated Recent-removal failure. |
| [`notes-mind-maps-live-validation-2026-08-28.md`](notes-mind-maps-live-validation-2026-08-28.md) | live report | Sanitized cross-backend note-backed mind-map classification and kind-safe Android deletion proof. |
| [`blutter-dart3.13.patch`](blutter-dart3.13.patch) | tooling | Port of [blutter](https://github.com/worawit/blutter) to Dart 3.13, needed to decompile this app's snapshot. |

`schema.proto` and `enums.txt` are **regenerable artifacts, not hand-written docs**.
They are committed because CI parses them; regenerate rather than hand-edit.
The generator is [`scripts/parse_pbschema.py`](../../scripts/parse_pbschema.py).

The reduced compile inputs used by the private Android adapters live under
`src/notebooklm/_android/proto_src/`. Regenerate their checked-in Python modules and the full
descriptor fixture with `python scripts/regenerate_android_protos.py --write`; use `--check` in CI.
The cumulative `orchestration_service.proto` owns the one exact generated service; individual
message overlays remain service-free. Its 18 admitted methods plus the 11 explicit signature
exceptions exhaustively equal the 29 implemented adapter paths.
The package, generated protos, and adapters remain private, direct-testable migration building
blocks. Normal `NotebookLMClient` assembly continues to select Web for every namespace; no client
factory branch selects Android Notes.

## Private Notes conformance

The authenticated conformance probe exercises the complete eight-method Notes manifest, including
ordinary note CRUD and note-backed mind-map list/delete. Use a dedicated profile that contains both
valid Web cookies and a sibling Android `master_token.json`, backed by an account on which disposable
notebooks may be created and deleted. The test prefixes and registers its resources, performs a final
prefix scan, and should pass twice against the same account to prove cleanup and rerun safety:

```bash
export NOTEBOOKLM_PROFILE=agent-b8p-notes
export NOTEBOOKLM_ANDROID_NOTES_CONFORMANCE=1
uv run pytest tests/e2e/test_android_notes_conformance.py -m e2e -vv
uv run pytest tests/e2e/test_android_notes_conformance.py -m e2e -vv
```

This test is opt-in and destructive only to the uniquely prefixed resources it creates. Keep the
profile isolated as described in the repository agent guidance, inspect the final cleanup result, and
do not treat a successful run as public-promotion evidence by itself. Three substitution blockers remain:

- Android exposes only last-edit timestamp evidence where the public `Note` model also exposes
  `created_at`; copying last-edit into creation time would be a semantic guess.
- Android `list_mind_maps` can project only the evidenced id/content/name/type/prompt/last-edit fields.
  The public method preserves Web's raw row boundary, whose additional metadata/source slots have not
  been proven wire-equivalent on Android.
- After Android deletion, Android exact-ID lookup reports absence while Web exact-ID lookup exposes the
  persisted soft-delete tombstone as the same note id with empty title/content. Both list projections
  exclude it, but the established `get_or_none` results are not substitutable.

Until exact capture evidence resolves all three gaps, the eight methods remain a private conformance target
and the Notes namespace remains Web in normal SDK, CLI, MCP, and REST assembly.

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
