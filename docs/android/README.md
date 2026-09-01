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

Start with the durable contracts, then follow the thematic evidence report for the feature you are
changing. Validation dates and the hashes of the former dated reports are retained inside each
consolidated document rather than encoded in filenames.

### Contracts and manifests

| File | Role |
|---|---|
| [`endpoints.md`](endpoints.md) | gRPC method surface and mobile ⇄ Web cross-reference |
| [`proto-evidence-ledger.md`](proto-evidence-ledger.md) | exact/local compile closure, replay policy, hashes, and admission decisions |
| [`schema.proto`](schema.proto) | generated 295-message / 767-field Dart-AOT recovery parsed by CI |
| [`enums.txt`](enums.txt) | generated 77-enum integer inventory parsed by CI |
| [`grpc-service-signature-inferences.json`](grpc-service-signature-inferences.json) | ten Web-derived signatures with conventional request/response type names |
| [`grpc-service-signature-exceptions.json`](grpc-service-signature-exceptions.json) | empty implemented-path exception manifest |
| [`grpc-runtime-parser-overrides.json`](grpc-runtime-parser-overrides.json) | exact paths intentionally decoded through local live-field overlays |

### Consolidated evidence

| File | Scope |
|---|---|
| [`artifact-contracts-and-live-validation.md`](artifact-contracts-and-live-validation.md) | generation families, representations, data tables, retry, mind maps, transfers, and Drive export |
| [`grpc-capability-and-signature-evidence.md`](grpc-capability-and-signature-evidence.md) | signed-APK inventory, Web-derived signatures, backend routing, and semantic probes |
| [`resource-lifecycle-and-public-qualification.md`](resource-lifecycle-and-public-qualification.md) | notebook copy/metadata, notes/maps, labels, collections, and public-selection boundaries |
| [`public-api-audit.md`](public-api-audit.md) | current implementation/rejection decisions across Android adapters |
| [`file-transfer-evidence.md`](file-transfer-evidence.md) | Scotty upload and artifact-download protocol with interception details |
| [`deep-research-evidence.md`](deep-research-evidence.md) | Deep Research wire contract, lifecycle, reproducer, and interception |
| [`copy-append-suggestion-evidence.md`](copy-append-suggestion-evidence.md) | live Android gRPC evidence for the #2283 family: `AddSourcesAsync`, `AppendSource`, `CopySourcesAsync`, `CopyArtifactsAsync`, `NextStepSuggestions`, `GetArtifactCustomizationChoices` |
| [`auth-research.md`](auth-research.md) | Android OAuth identity, scopes, and bearer validation |
| [`blutter-grpc-signature-evidence.md`](blutter-grpc-signature-evidence.md) | exact generated-client bindings for formerly unresolved response FQNs |

### Capture and tooling

| File | Role |
|---|---|
| [`capture.md`](capture.md) | primary HTTP/2 gRPC interception runbook |
| [`android-traffic-capture.md`](android-traffic-capture.md) | legacy rooted-emulator Cronet/Frida procedure |
| [`blutter-dart3.13.patch`](blutter-dart3.13.patch) | Dart 3.13 port used to decompile the app snapshot |

`schema.proto` and `enums.txt` are **regenerable artifacts, not hand-written docs**.
They are committed because CI parses them; regenerate rather than hand-edit.
The generator is [`scripts/parse_pbschema.py`](../../scripts/parse_pbschema.py).

```bash
uv run python scripts/parse_pbschema.py /path/to/blutter/out/full/asm \
  > docs/android/schema.proto
```

The default package-directory selectors preserve the complete 54-file historical evidence scope.
The generator reports `295 messages, 767 fields` and resolves package identity through the sibling
`objs.txt`; an unresolved package remains explicit rather than being inferred from its directory.

The reduced compile inputs used by the internal Android adapters live under
`src/notebooklm/_android/proto_src/`. Regenerate their checked-in Python modules and the full
descriptor fixture with `python scripts/regenerate_android_protos.py --write`; use `--check` in CI.
The cumulative `orchestration_service.proto` owns the 47-method orchestration service;
`sharing.proto` owns the separately proven two-method exact sharing service, and individual
orchestration message overlays remain service-free. Ten orchestration signatures are explicitly
marked as web-derived conventional-name inferences; all other generated signatures are exact.
The 49 generated methods exhaustively equal the 49 implemented adapter paths, and the signature
exception manifest is empty. Generated descriptors, adapter paths, inference provenance, and the
hash-pinned external method manifest are checked in both
directions, so a locally repeated claim cannot admit a normalized or unresolved response type.
The package and generated protos remain private implementation details. Explicit
`backend="android"` selection installs Android adapters for all eleven public namespaces. The
adapters use native Android gRPC/Scotty wherever the mobile contract is usable and isolate the
remaining handler gaps behind narrow Web compatibility collaborators. There are exactly three:

- notebook recent-removal, whose exact mobile route consistently rejects valid owned resources;
- CSV/DOCX file upload, whose exact mobile transaction finalizes but processing reaches
  `UNKNOWN`/`ERROR`; and
- sharing view-level mutation, whose separate `MutateProject` branch remains rejected.

Artifact mind-map generation, source refresh and Drive download/upload, account settings,
collaborator sharing, and automatic labels now remain native under Android selection. The settings
probe temporarily changed a pre-existing output language, verified the mutation and native
readback, then restored and re-verified the original value in `finally`. Collaborator writes are
bundle- and byte-contract-qualified; no live invitation was sent without a controlled secondary
identity.

`client.backends` describes the installed namespace adapters, so every entry is `android`; it does
not claim that each internal operation has a native mobile RPC.

## Public Collections qualification

Collections is selected only for explicit `backend="android"`. Its permanent live gate exercises
all nine public methods and cleans up one uniquely named notebook and collection. Run the same gate
twice against an isolated profile containing both Web cookies and a sibling master token:

```bash
export NOTEBOOKLM_PROFILE=android-collections-e2e
export NOTEBOOKLM_ANDROID_COLLECTIONS_CONFORMANCE=1
uv run pytest tests/e2e/test_android_collections_conformance.py -m e2e -vv
uv run pytest tests/e2e/test_android_collections_conformance.py -m e2e -vv
```

## Android Notes conformance

The authenticated conformance probe exercises the complete eight-method Notes manifest, including
ordinary note CRUD and note-backed mind-map list/delete. Use a dedicated profile that contains both
valid Web cookies and a sibling Android `master_token.json`, backed by an account on which disposable
notebooks may be created and deleted. The test prefixes and registers its resources, performs a final
prefix scan, and should pass twice against the same account to prove cleanup and rerun safety:

```bash
export NOTEBOOKLM_PROFILE=android-notes-e2e
export NOTEBOOKLM_ANDROID_NOTES_CONFORMANCE=1
uv run pytest tests/e2e/test_android_notes_conformance.py -m e2e -vv
uv run pytest tests/e2e/test_android_notes_conformance.py -m e2e -vv
```

This test is opt-in and destructive only to the uniquely prefixed resources it creates. Keep the
profile isolated as described in the repository agent guidance and inspect the final cleanup result.
The Android projection preserves only evidenced semantics: unknown creation time is `None`, raw
mind-map rows contain the public contract's supported `[id, content]` prefix, and exact absence after
deletion returns `None`. The Web soft-delete tombstone is a storage leak rather than a documented
`get_or_none` guarantee, so Android does not fabricate it.

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
