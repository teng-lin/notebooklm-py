# Web-bundle-derived mobile gRPC signatures

**Audit date:** 2026-08-29  
**Authenticated bundle SHA-256:** `8cc2569196b28083ba58a33319df79af97ec1832f442c4a182289894edf5eaef`

The current authenticated NotebookLM web bundle confirms all 48 project RPC IDs used by this
repository, including the seven methods absent from both audited Android APKs. The web registry
constructor order is response first, request second:

```text
registration(rpc_id, ResponseCtor, RequestCtor, [..., "/Service.Method"])
```

Call sites independently establish that order: they construct the third registration argument and
decode the awaited value with the second. This corrects the former reversed-order comment in
`scripts/capture_rpc_registry.py`.

## Signature matrix

| RPC ID | Generated mobile signature | Bundle/mobile evidence | Confidence |
|---|---|---|---|
| `te3DCe` | `CopyProject(CopyProjectRequest) returns (Project)` | request context/source project/title at `#1/#2/#3`; response ctor is identical to `CreateProject`/`MutateProject`; successful mobile copy | `Project` exact by ctor identity; request name conventional |
| `b7Wfje` | `MutateSource(MutateSourceRequest) returns (MutateSourceResponse)` | request contains `SourceId #2`, repeated mutations `#3`, and context `#4`; response ctor wraps `Source #1`; successful mobile mutation/read-back | type names conventional; admitted branch/wrapper pinned, other mutation variants reserved |
| `ciyUvf` | `GenerateReportSuggestions(GenerateReportSuggestionsRequest) returns (GenerateReportSuggestionsResponse)` | request context/project/repeated source IDs at `#1/#2/#3`; response repeated suggestions `#1`, with admitted title/description/repeated source IDs/prompt/audience at `#1/#2/#4/#5/#6`; an accessed nested field `#3` remains unknown | type names conventional; admitted partial wire shape pinned |
| `agX4Bc` | `CreateLabel(CreateLabelRequest) returns (CreateLabelResponse)` | request context `#1`; dedicated response ctor with repeated labels `#2`; heterogeneous collection rows at `#3` remain unknown; manual-create request and mobile CRUD/read-back succeeded | type names conventional; admitted partial wire shape pinned |
| `le8sX` | `MutateLabel(MutateLabelRequest) returns (MutateLabelResponse)` | request context `#1`; dedicated response ctor distinct from protobuf `Empty`; no response fields are retained/used; property/membership mobile read-back succeeded | type names conventional; no-field partial parser inferred |
| `GyzE7e` | `DeleteLabels(DeleteLabelsRequest) returns (DeleteLabelsResponse)` | request context `#1`; dedicated response ctor distinct from protobuf `Empty`; no response fields are retained/used; mobile delete/read-back succeeded | type names conventional; no-field partial parser inferred |
| `Zbrupe` | `CancelDiscoverSourcesJob(CancelDiscoverSourcesJobRequest) returns (google.protobuf.Empty)` | request context `#1`; response ctor is identical to `DeleteProjects`, `DeleteSources`, `DeleteArtifact`, and `DeleteChatTurns`, whose Android bindings independently prove `google.protobuf.Empty`; mobile cancel lifecycle succeeded | request type exact from pinned package source and augmented with current context field; response exact by ctor bridge |

The six rows marked conventional are deliberately recorded in
`grpc-service-signature-inferences.json`. Protobuf type names are not serialized on the wire, so
successful mobile calls prove message layout but cannot alone prove those six names. They are now
generated to provide the complete callable service requested by the project, without relabeling
them as APK-extracted facts.

Constructor identity is also preserved inside the organization request closure: manual-create
properties, mutate properties, and the four add/remove source/notebook payloads use distinct
generated message types even though their currently observed bytes are structurally equivalent.
The empty partial parsers for mutate/delete responses mean only that the bundle retained no fields;
they do not claim the remote messages are literally fieldless.

The APK absence result remains unchanged: the official clients simply do not compile these seven
call sites. APK inventory is a client-feature inventory, not a mobile-backend capability list.
