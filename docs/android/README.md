# Android backend and protocol evidence

`notebooklm-py` supports the default **Web** `batchexecute` backend and an
explicit **Android** backend over the same product's bearer-authenticated gRPC
services. The mobile protobuf surface is also a useful oracle for Web's
positional payloads. This directory combines the Android user entry point with
the protocol contracts and evidence used to maintain both transports.

Start with the [Web/Android comparison](https://teng-lin.github.io/notebooklm-py/diagrams/06-backends-web-and-android.html), then open the
[Android subsystem](https://teng-lin.github.io/notebooklm-py/diagrams/14-android-backend.html) and
[call sequence](https://teng-lin.github.io/notebooklm-py/diagrams/15-android-call-path.html). Profile/backend precedence and
credential-scoped transfers are covered by diagrams
[28](https://teng-lin.github.io/notebooklm-py/diagrams/28-profile-auth-backend-selection.workflow.html) and
[30](https://teng-lin.github.io/notebooklm-py/diagrams/30-transfer-security-boundaries.dataflow.html).

## Using the Android backend

Install the Android runtime (and the browser extra for the one-time interactive
master-token bootstrap), then create a durable credential in the selected
profile:

```bash
pip install "notebooklm-py[android]"
pip install "notebooklm-py[browser]"  # one-time interactive bootstrap only
notebooklm login --master-token --account you@example.com
```

Select Android per CLI invocation, for the process, or in Python:

```bash
notebooklm --backend android list --json
NOTEBOOKLM_BACKEND=android notebooklm list --json
```

```python
from notebooklm import NotebookLMClient

async with NotebookLMClient.from_storage(backend="android") as client:
    notebooks = await client.notebooks.list()
```

Web remains the default when no backend is selected. Explicit Android selection
installs Android implementations for all 11 typed namespaces and performs no
typed-operation fallback to Web. Use the backend-selected `client.raw.unary(...)`
or `client.raw.unary_stream(...)` for advanced Android calls. The deprecated
`client.rpc_call(...)` wrapper still takes Web `RPCMethod` identifiers; its first
Android use opens a separate Web compatibility sidecar through v0.x.
The Android `from_storage(...)` bootstrap is read-only for cookies: it performs
no PSIDTS poke/recovery or profile-cookie merge. A homepage cookie observation
remains in memory until the deprecated sidecar takes ownership, if ever.

`master_token.json` is a durable, full-account credential that can mint OAuth
tokens for multiple Google services and survives a password change. Prefer a
dedicated account, protect the selected profile, never log or commit the token,
and revoke the associated device/session in Google Account security if it may
have leaked. See the full [installation and authentication guide](../installation.md#d-headless-server-or-ci).

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
| [`schema.proto`](schema.proto) | generated 323-message / 868-field Dart-AOT recovery parsed by CI |
| [`enums.txt`](enums.txt) | generated 104-block (94 enum names) integer inventory parsed by CI |
| [`grpc-service-signature-inferences.json`](grpc-service-signature-inferences.json) | seventeen Web-derived signatures with conventional request/response type names |
| [`grpc-service-signature-exceptions.json`](grpc-service-signature-exceptions.json) | empty implemented-path exception manifest |
| [`grpc-runtime-parser-overrides.json`](grpc-runtime-parser-overrides.json) | exact paths intentionally decoded through local live-field overlays |

### Consolidated evidence

| File | Scope |
|---|---|
| [`artifact-contracts-and-live-validation.md`](artifact-contracts-and-live-validation.md) | generation families, representations, data tables, retry, mind maps, transfers, and Drive export |
| [`grpc-capability-and-signature-evidence.md`](grpc-capability-and-signature-evidence.md) | signed-APK inventory, Web-derived signatures, backend routing, and semantic probes |
| [`resource-lifecycle-and-public-qualification.md`](resource-lifecycle-and-public-qualification.md) | notebook copy/metadata, notes/maps, labels, collections, and public-selection boundaries |
| [`public-api-audit.md`](public-api-audit.md) | 2026-08-29 rejection-audit snapshot; its three compatibility seams were later closed |
| [`web-compat-seam-closure.md`](web-compat-seam-closure.md) | current zero-Web-operation-collaborator boundary and the evidence that closed the final seams |
| [`file-transfer-evidence.md`](file-transfer-evidence.md) | Scotty upload and artifact-download protocol with interception details |
| [`deep-research-evidence.md`](deep-research-evidence.md) | Deep Research wire contract, lifecycle, reproducer, and interception |
| [`copy-append-suggestion-evidence.md`](copy-append-suggestion-evidence.md) | live Android gRPC evidence for the #2283 family: `AddSourcesAsync`, `AppendSource`, `CopySourcesAsync`, `CopyArtifactsAsync`, `NextStepSuggestions`, `GetArtifactCustomizationChoices` |
| [`source-search-evidence.md`](source-search-evidence.md) | live Web and Android wire evidence for `RetrieveRelevantChunks` / `sources.search` |
| [`auth-research.md`](auth-research.md) | Android OAuth identity, scopes, and bearer validation |
| [`blutter-grpc-signature-evidence.md`](blutter-grpc-signature-evidence.md) | exact generated-client bindings for formerly unresolved response FQNs |
| [`chat-session-control-evidence.md`](chat-session-control-evidence.md) | live Web/Android session-status and cancellation semantics for #2303 |

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
uv run python scripts/parse_pbschema.py /path/to/blutter/out/<build>/asm \
  > docs/android/schema.proto
uv run python scripts/parse_pbenums.py /path/to/blutter/out/<build> \
  > docs/android/enums.txt
```

The default package-directory selectors preserve the complete historical evidence scope (66 files
in the current dump). The schema generator reports `323 messages, 868 fields` and resolves package
identity through the sibling `objs.txt`; an unresolved package remains explicit rather than being
inferred from its directory. In particular `FunctionCall`, `FunctionResponse`, `TailwindStruct`,
and `TailwindValue` sit in Dart libraries under an `orchestration.v1.agency` directory but are
registered with the `google.internal.labs.tailwind.orchestration.v1` `PackageName` object, and no
`…agency` package object exists in either dump; the schema records the registered package. Nested
messages keep the dotted name handed to `BuilderInfo` in their `// Protobuf FQN:` line
(`…TailwindStruct.TailwindStructEntry`) while the `message` identifier stays the Dart class name.

The enum generator merges the object pool with the object store and emits **one block per
(Dart library, enum class)**: ten class names (`ArtifactType`, `DiscoveryMode`,
`OriginalSourceContentType`, `UserAction`, …) are declared by two libraries with different
integers, and a class-name-only merge would let one silently shadow the other. Each header names
its `[library …]`; `[objs adds …]` lists the integers that only the object store proved,
`[objs-ONLY]` marks an enum with no pool object at all, and `[aliases …]` would flag an integer
carrying two names inside one enum (none today). The guardrail loader keeps only the wire-library
blocks for a shared name and raises if two of those disagree.

### Current regeneration identity

Both artifacts were regenerated from this build (verified from the binary, not assumed):

| Item | Value |
|---|---|
| App | Gemini Notebook (NotebookLM) `1.55.10.971450265` (`versionCode=153888`), posted 2026-08-29 |
| AOT library | `lib/arm64-v8a/libNotebookLM_prod_android_library_flutter_artifacts.so` |
| AOT library SHA-256 | `77bff7507e393c092b78ff1756bb3d726881050b22728dcc8c46cf0fecd7cda7` |
| Dart SDK | `3.14.0-166.0.dev` (dev channel), snapshot hash `8c325a9e3a1c32ffd39325f735c49133` |
| Regenerated | 2026-09-01 |

The `1.46.7` snapshot (`082d75e3…`, Dart `3.13.0-256.0.dev`) remains the basis for the dated
capture reports, the version-scoped method manifest, and
[`blutter-grpc-signature-evidence.md`](blutter-grpc-signature-evidence.md). The checked-in
[`blutter-dart3.13.patch`](blutter-dart3.13.patch) targets Dart 3.13; the Dart 3.14 build used for
this regeneration is not yet captured as a patch.

The reduced compile inputs used by the internal Android adapters live under
`src/notebooklm/_android/proto_src/`. Regenerate their checked-in Python modules and the full
descriptor fixture with `python scripts/regenerate_android_protos.py --write`; use `--check` in CI.
The cumulative `orchestration_service.proto` owns the 57-method orchestration service;
`sharing.proto` owns the separately proven two-method exact sharing service, and individual
orchestration message overlays remain service-free. Seventeen orchestration signatures are explicitly
marked as web-derived conventional-name inferences; all other generated signatures are exact.
The 59 generated methods exhaustively equal the 59 implemented adapter paths, and the signature
exception manifest is empty. Generated descriptors, adapter paths, inference provenance, and the
hash-pinned external method manifest are checked in both
directions, so a locally repeated claim cannot admit a normalized or unresolved response type.
The package and generated protos remain private implementation details. Explicit
`backend="android"` selection installs Android adapters for all eleven public namespaces. The
installed Android namespace graph has no Web operation collaborators. Native Android gRPC/Scotty,
bearer-authenticated assets, Drive staging, and local composition cover the typed public contract:
recent-removal uses the native shared-project route and treats the owned-project `INTERNAL` result
as the same already-absent no-op exposed by Web; CSV/DOCX/PPTX uploads stage through Drive when the
mobile Scotty frontend cannot parse the format; and sharing view level uses the native
`MutateProject` tag-9 branch. The evidence and bounded divergences are recorded in
[`web-compat-seam-closure.md`](web-compat-seam-closure.md).

Artifact mind-map generation, source refresh and Drive download/upload, account settings,
collaborator sharing, and automatic labels now remain native under Android selection. The settings
probe temporarily changed a pre-existing output language, verified the mutation and native
readback, then restored and re-verified the original value in `finally`. Collaborator writes are
bundle- and byte-contract-qualified; no live invitation was sent without a controlled secondary
identity.

`client.backends` describes the installed namespace adapters, so every entry is `android`. It does
not reparameterize the deprecated `client.rpc_call(...)` wrapper, whose `RPCMethod` identifiers
remain Web batchexecute-specific, or imply that every Android workflow is a single gRPC call. The
wrapper's compatibility sidecar never starts Web keepalive and is removed with the wrapper in v1.0.

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
868 fields. Do not treat it as real.

**Several messages appear twice with *different* tags.** One copy is the wire
schema (`…orchestration.v1`, `…tailwind.v1`), the other is the app's local
persistence schema (`…mobile.app.protos.persistence`). Always scope a lookup to
the right package; the guardrail refuses ambiguous matches rather than guessing.

**Use the merged enum dump, not the object pool alone.** The snapshot object pool
inlines objects for 102 of the 104 (library, class) blocks and only a fraction of their members;
merging it with the object store yields the full 104 blocks / 94 class names / 2180 values.
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
