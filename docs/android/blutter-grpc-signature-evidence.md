# Blutter gRPC signature evidence

**Evidence date:** 2026-08-29
**Official app:** NotebookLM Android `1.46.7.940945420` (`versionCode=138238`)
**Package:** `com.google.android.apps.labs.language.tailwind`
**AOT library SHA-256:** `082d75e36eb03aea7ea5a8c252029c48b964177311ca4ebac6392814b8e6f81f`

This report closes six response-FQN gaps using the official app's decompiled generated-client
bindings. It does not infer a response type from zero response bytes. For every admitted row, the
same recovered client method contains the full gRPC path, its
`RpcClientMethod<Request, Response>` type arguments, and the concrete response-constructor closure.

The two compact blutter identity inputs are:

| Raw input | SHA-256 | Role |
|---|---|---|
| `out/full/pp.txt` | `2fc0bad6bee700cb628deb9ac1922eeea3d1255b51d8d2e1f63c5537d98965b0` | adjacent method path, request/response type arguments, and response constructor |
| `out/full/ida_script/addNames.py` | `982fcbf1c5ef1d7d0aa9d5d0ae8af3c6e6a7c575af9bdba1fc3d7469aa8bc511` | exact Dart protobuf library identity for each response class |

Reproduce the deterministic method ledger from a matching blutter output tree:

```bash
uv run python scripts/extract_blutter_grpc_signatures.py \
  /path/to/out/full/pp.txt /path/to/out/full/ida_script/addNames.py
```

## Exact recovered bindings

| Full method | Recovered generic binding | Exact response FQN | `pp.txt` lines |
|---|---|---|---|
| `.../DeleteArtifact` | `<DeleteArtifactRequest, Empty>` | `.google.protobuf.Empty` | 32881–32885 |
| `.../DeleteNotes` | `<DeleteNotesRequest, DeleteNotesResponse>` | `.google.internal.labs.tailwind.orchestration.v1.DeleteNotesResponse` | 40987–40991 |
| `.../DeleteChatTurns` | `<DeleteChatTurnsRequest, Empty>` | `.google.protobuf.Empty` | 41117–41120 |
| `.../DeleteProjects` | `<DeleteProjectsRequest, Empty>` | `.google.protobuf.Empty` | 41151–41154 |
| `.../DeleteSources` | `<DeleteSourcesRequest, Empty>` | `.google.protobuf.Empty` | 41228–41231 |
| `/labs.language.tailwind.sharing.LabsTailwindSharingService/ShareProject` | `<ShareProjectRequest, ShareProjectResponse>` | `.labs.language.tailwind.sharing.ShareProjectResponse` | 47540–47544 |

The disassembled method bodies independently repeat the full path, generic binding, and constructor
at `tailwind_rpc_client.dart` lines 925/935/942, 4019/4029/4036, 4721/4731/4738,
4862/4872/4879, and 5144/5154/5161. The sharing binding appears at
`tailwind_sharing_rpc_client.dart` lines 222/232/239.

Package identity is not derived from a directory-name heuristic. The recovered class identities in
`addNames.py` name `google.protobuf$empty.pb_Empty`,
`google.internal.labs.tailwind.orchestration.v1$labs_tailwind_orchestration_service.pb_DeleteNotesResponse`,
and `labs.language.tailwind.sharing$labs_tailwind_sharing_service.pb_ShareProjectResponse`.
The corresponding zero-field `BuilderInfo` classes are also retained by the corrected
`scripts/parse_pbschema.py` extractor.

The deterministic `scripts/extract_blutter_grpc_signatures.py` pass resolves 48 unique generated
client bindings from this dump. In addition to the six implemented-path promotions above, it
closes inventory-only response FQNs for `LogInteractionEvent`, `MutateAccount`,
`RemoveRecentlyViewedProject`, `SubmitFeedback`, and sharing `CreateAccessRequest`. Their existing
private/excluded dispositions do not change. `UpsertArtifactUserState` remains in the binary method
inventory without a recovered adjacent generic binding and is still explicitly unresolved.

## Boundary

The same APK has no generated client bindings for `CopyProject`, `MutateSource`,
`GenerateReportSuggestions`, `CreateLabel`, `MutateLabel`, `DeleteLabels`, or
`CancelDiscoverSourcesJob`. This APK report therefore makes no package claim for them. Subsequent
current-web-bundle constructor tracing plus successful mobile-backend calls promoted all seven;
six conventional type-name inferences are recorded separately in
[`grpc-service-signature-inferences.json`](grpc-service-signature-inferences.json), while
`CancelDiscoverSourcesJob` closes exactly through a shared `google.protobuf.Empty` constructor.
A second audit of the newer Google-signed
`1.55.10.971450265` build found zero occurrences of all seven across its AOT, Dex, resource, native,
and reconstructed blutter trees; see
[`grpc-capability-and-signature-evidence.md`](grpc-capability-and-signature-evidence.md).
