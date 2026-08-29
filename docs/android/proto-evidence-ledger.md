# Android protobuf evidence ledger

**Status:** admitted B1 read closure plus B2 notebook, B3 source, and B4 artifact overlays

**Evidence snapshot:** 2026-08-27

**Scope:** B1 project/source reads, B2 notebook operations, the B3 URL/maintenance/flat-content
slice, and B4 artifact list/get/create/update/delete plus its repository-local wire-equivalent
report-suggestion overlay

This ledger is the admission boundary for `src/notebooklm/_android/proto_src/`. The recovered
[`schema.proto`](schema.proto) is Dart-AOT evidence, not a compile input: it flattened several
libraries and contains duplicate package-local persistence declarations. The checked-in B1 proto
sources instead preserve the exact wire packages from the descriptor/Dart library boundary and
copy only the fields below. Missing fields remain protobuf unknown fields; they are not filled from
plausible-looking flattened declarations.

## Evidence input identities

The external exact-package snapshot was reviewed, reduced to the admitted B1/B4 fields, and then made self-contained by
this ledger, the checked-in proto sources, descriptor set, and synthetic fixtures. Hashes prevent a
later local checkout from silently changing what was admitted.

| Evidence input | SHA-256 | Role |
|---|---|---|
| exact-package orchestration `supported.proto` | `829c4ee871fd66421ee098fa266793ec68773e625ff005cc519b2c0f7c191ae9` | service/message FQNs, package, tags, cardinality, import origin |
| exact-package `source_settings.proto` | `becd695c4281e23064c16fc1441c61117e5dc2a44c52cadf44af9e31c7cb8b18` | separate settings package, fields #2/#4, complete enums |
| [`schema.proto`](schema.proto) | `aa9f49d302ff9a64cc16d08b2f2f9031f77a348b3707dd98df37be91a67355ec` | flattened Dart recovery used to identify gaps, never as a compile input; hash includes the curated `docs/android/` path comments |
| [`enums.txt`](enums.txt) | `8c8137c1842d07b54ba9e52feeea7c3ce09246415c26d964d17bec68eee228bc` | exhaustive enum names and integers |
| exact method manifest | `c2cf4bf2e6cdefd35232f01572070fbe07d11ef9bad99b556f76b5e3748f38a3` | full method paths, request/response FQNs, unary cardinality |
| [`file-transfer-live-validation-2026-08-27.md`](file-transfer-live-validation-2026-08-27.md) | `c713a7cfe5058482aa8fc9a0201ad08487296700223f23829842795f85713107` | live `ListArtifacts` representations and direct infographic PNG transfer |
| [`web-parity-gap-live-validation-2026-08-27.md`](web-parity-gap-live-validation-2026-08-27.md) | `c0a3a16b2ff0eba18395e5a53ae2ebddb3b299d8b2cae0d6d868a3e294b08251` | live delete, rename/read-back and report-suggestion response cardinality |
| [`endpoints.md`](endpoints.md) | `57467b424515cf0dfa4c3e08c636ab6a8d0bfddbe5701cf50675ea39338e4e62` | live request/response envelopes and route results |

The recovery method and the warning about duplicate packages are committed in
[`README.md`](README.md#caveats-that-will-bite-you). Live request/response shapes are documented in
[`endpoints.md`](endpoints.md#getproject).

## Service ledger

| Full method | Request FQN | Response FQN | Cardinality | Request fields populated by B1 |
|---|---|---|---|---|
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GetProject` | `.google.internal.labs.tailwind.orchestration.v1.GetProjectRequest` | `.google.internal.labs.tailwind.orchestration.v1.GetProjectResponse` | unary/unary | `project_id #1`, `include_audio_overview_ids #2`; no `RequestContext` |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/ListRecentlyViewedProjects` | `.google.internal.labs.tailwind.orchestration.v1.ListRecentlyViewedProjectsRequest` | `.google.internal.labs.tailwind.orchestration.v1.ListRecentlyViewedProjectsResponse` | unary/unary | `include_own_projects #2`, `include_audio_overview_ids #3`; no `RequestContext` |

The exact-package service descriptor contains exactly these two methods in B1. B2 does not widen
that descriptor: its captures prove method paths and serialized layouts, but not the complete
package/import identity of every reachable request/response message. B2 therefore uses the
repository-local wire overlay below with manual full-path calls.

## B2 notebook method ledger

`Wire*` means a message in the deliberately non-Google package
`notebooklm.internal.android.wire.v1`. It claims wire equivalence only. Bare `Project` responses
reuse the B1 exact-package message, and empty deletion uses `google.protobuf.Empty` directly.

| Full method | Request parser | Response parser | Replay | Evidence |
|---|---|---|---|---|
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/CreateProject` | `WireCreateProjectRequest` | exact-package `Project` (bare) | never in transport; base create probes before retry | [`endpoints.md`](endpoints.md#createproject--create-a-notebook) |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/DeleteProjects` | `WireDeleteProjectsRequest` | `google.protobuf.Empty` | never | [`endpoints.md`](endpoints.md#deleteprojects--delete-notebooks) |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/MutateProject` | `WireMutateProjectRequest` | exact-package `Project` (bare) | never | [`endpoints.md`](endpoints.md#mutateproject--rename--edit-notebook-fields) |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/CopyProject` | `WireCopyProjectRequest` | exact-package `Project` (bare) | never; transport ambiguity is surfaced | [copy validation](labels-collections-copy-mobile-grpc-2026-08-27.md#copy-a-notebook) |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GenerateNotebookGuide` | `WireGenerateNotebookGuideRequest` | `WireGenerateNotebookGuideResponse` | never; stateful | [`endpoints.md`](endpoints.md#generatenotebookguide) |

## B4 service ledger

Every Google-package FQN, field name, tag, type and cardinality compiled in
`b4_artifacts.proto` was independently checked against the exact-package archived
`supported.proto` whose SHA-256 is pinned above. References to the flattened `schema.proto` in
source comments are corroborating Dart-symbol evidence, never the authority for a Google FQN.
The B4 overlay intentionally declares no second protobuf `service`: protobuf cannot reopen the
same service across files, and widening B1's two-method generated stub would make the reviewed B1
closure unstable. `AndroidSession` dispatches these exact unary paths with the ledgered message
classes.

| Full method | Exact request FQN | Exact response FQN | B4 disposition |
|---|---|---|---|
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/ListArtifacts` | `.google.internal.labs.tailwind.orchestration.v1.ListArtifactsRequest` | `.google.internal.labs.tailwind.orchestration.v1.ListArtifactsResponse` | admitted safe read; one call per Studio list/poll tick |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GetArtifact` | `.google.internal.labs.tailwind.orchestration.v1.GetArtifactRequest` | `.google.internal.labs.tailwind.orchestration.v1.GetArtifactResponse` | message closure admitted; common SDK `get` remains concrete over aggregate `list` |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/CreateArtifact` | `.google.internal.labs.tailwind.orchestration.v1.CreateArtifactRequest` | `.google.internal.labs.tailwind.orchestration.v1.CreateArtifactResponse` | quiz-only mutation, never replayed |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/UpdateArtifact` | `.google.internal.labs.tailwind.orchestration.v1.UpdateArtifactRequest` | `.google.internal.labs.tailwind.orchestration.v1.Artifact` | title-only mutation with etag, never replayed, then list read-back |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/DeleteArtifact` | `.google.internal.labs.tailwind.orchestration.v1.DeleteArtifactRequest` | `.google.protobuf.Empty` | never replayed; sanitized `NOT_FOUND` is idempotent success |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GenerateReportSuggestions` | APK-absent; repository-local `GenerateReportSuggestionsRequestWire` | APK-absent; repository-local `GenerateReportSuggestionsResponseWire` | safe live-added read; no Google message-FQN claim |

## B4 exact artifact field ledger

This table is exhaustive for `b4_artifacts.proto`; fields present in the archived message but not
needed by B4 are deliberately left unknown. “Exact” means the pinned exact-package archive, not a
field inferred from its plausible Dart name.

| Exact-package message | Admitted fields (`name #tag`, cardinality and type) | B4 use |
|---|---|---|
| `MediaStreamingUrl` | `url #1` string; `type #2` `MediaStreamingType` | audio/video representation projection |
| `QuizGenerationOptions` | `question_quantity #1` `QuestionQuantity`; `quiz_difficulty #2` `QuizDifficulty` | quiz create options |
| `AppArtifactGenerationOptions` | `app_type #1` `AppType`; `free_text_steering_prompt #3` string; `quiz_generation_options #8` message | quiz kind/prompt/create |
| `AppArtifact` | `generation_options #2` message | app kind/options; HTML/content fields remain unknown and unsupported |
| `AudioOverviewGenerationOptions` | `episode_focus #1` string | listing prompt |
| `AudioOverviewArtifact` | `generation_options #2` message; repeated `media_urls #6`; `duration #7` `google.protobuf.Duration` | listing projection only |
| `ExplainerVideoGenerationOptions` | `video_focus #3` string | listing prompt |
| `ExplainerVideoArtifact` | `generation_options #3` message; repeated `media_urls #5`; `duration #6` `google.protobuf.Duration` | listing projection only |
| `TailoredReportArtifactGenerationOptions` | `type #1` string; `document_directive #6` string | report kind/prompt |
| `TailoredReportArtifact` | `generation_options #2` message | listing projection only |
| `ServedImage` | `url #1` string | infographic/slide representation |
| `InfographicGenerationOptions` | `user_steering_prompt #1` string | listing prompt |
| `Infographic` | `title #1` string; `image #2` `ServedImage` | PNG selection |
| `InfographicArtifact` | `generation_options #1` message; repeated `infographics #3` | listing/download projection |
| `SlidesGenerationOptions` | `user_steering_prompt #1` string | listing prompt |
| `Slide` | `image #1` `ServedImage` | listing projection |
| `SlidesArtifact` | `generation_options #1` message; repeated `slides #3`; `pdf_download_url #4` string | listing projection only; unsupported PPTX download retains no compiled-only field |
| `FileArtifact` | `file_preview_url #3`, `file_download_url #4` strings | listing representation projection only |
| `ArtifactSource` | `source_id #1` imported exact `SourceId` | source IDs and quiz request |
| `Artifact` | `artifact_id #1` string; `title #2` string; `type #3` `ArtifactType`; repeated `sources #4`; `status #5` `ArtifactStatus`; `audio_overview #7`; `tailored_report #8`; `explainer_video #9`; `app #10`; `last_modified_timestamp #11` Timestamp; `infographic #15`; `slides #17`; `etag #22` string; `file #25` | the complete B4 public projection and mutation subset |
| `CreateArtifactRequest` / `Response` | request `project_id #2`, `artifact #3`; response `artifact #1` | quiz mutation |
| `GetArtifactRequest` / `Response` | request `artifact_id #1`; response `artifact #1` | exact closure, not separately dispatched by common `get` |
| `ListArtifactsRequest` / `Response` | request `project_id #2`; response repeated `artifacts #1` | Studio listing and polling |
| `UpdateArtifactRequest` | `artifact #1`; `update_mask #2` FieldMask; `etag #3` string | title rename |
| `DeleteArtifactRequest` | `artifact_id #2` string | idempotent delete |

The four exact enums are also pinned exhaustively by generated descriptor tests: `ArtifactType`
0–10, `ArtifactStatus` 0–6, `AppType` 0–5 and `MediaStreamingType` 0–4. The two nested quiz enums
are 0–3. Unknown future integers remain unknown rather than being coerced to a known family.

### B4 quiz request

The successful quiz branch sends `CreateArtifactRequest.project_id #2` and `artifact #3`, with
`Artifact.type = ARTIFACT_TYPE_APP`, repeated `Artifact.sources #4`, and
`AppArtifact.generation_options.app_type = APP_TYPE_QUIZ`. Quantity/difficulty use the exact nested
enums at quiz option fields #1/#2; free text uses app option field #3. No other family builder is
admitted and every unsupported public generation method rejects before source-ID resolution.

### B4 live-added report suggestion overlay

`GenerateReportSuggestions` is live-successful but absent from the APK method/symbol archive. B4
therefore compiles `notebooklm.android.internal.v1` `*Wire` messages, not Google-package symbols.
The live request pins project UUID field #2 and the response pins repeated suggestion field #1.
The nested wire row uses the response-observed web-equivalent semantic positions: title #1,
description #2, prompt #5 and audience level #6. This is explicitly a repository-local decoding
contract admitted for direct tests; it must be replaced, not renamed into a Google FQN, if an exact
descriptor later appears. Optional request context #1 and source filter #3 are omitted because B4
does not send them.

## B4 representation-transfer evidence boundary

Only response-provided URLs are projected: audio field #6, video field #5, infographic image #2,
slide image #1, slide PDF #4 and file preview/download #3/#4. The first implemented byte
path is the live-verified infographic PNG from `lh3.googleusercontent.com`; no URL is synthesized
from an artifact kind. `alr=yes` application redirects are defensive evidence from the
unauthenticated control. Signed GCS bearer stripping is a fail-closed policy, not a claim that a
signed-GCS authenticated branch was observed.

## Import closure

| Import | Exact package | Why reachable |
|---|---|---|
| `google/internal/labs/tailwind/v1/source_settings.proto` | `google.internal.labs.tailwind.v1` | `Source.settings #4`; Dart library boundary is `source_settings.pb.dart` |
| `google/protobuf/timestamp.proto` | `google.protobuf` | `ProjectMetadata.create_time #9` |

No common, agency, artifact, chat, mutation, context, Empty, Struct, Duration, or FieldMask proto is
reachable from the B1 read projection.

## Field ledger

`singular` means ordinary proto3 scalar/message presence; `repeated` is the only repeated
cardinality admitted. Every row is asserted exhaustively against the generated descriptors.

| Package.Message | Field | Tag | Cardinality | Type/evidence |
|---|---|---:|---|---|
| `orchestration.v1.SourceId` | `id` | 1 | singular | string; exact-package closure + Dart symbol |
| `orchestration.v1.WebpageMetadata` | `url` | 1 | singular | string; exact-package closure + Dart symbol |
| `orchestration.v1.GoogleDriveSourceMetadata` | `document_id` | 1 | singular | string; exact-package closure + Dart symbol |
| `orchestration.v1.GoogleDriveSourceMetadata` | `mime_type` | 3 | singular | string; exact-package closure + Dart symbol |
| `orchestration.v1.SourceMetadata` | `original_source_content_type` | 5 | singular | `OriginalSourceContentType`; complete values in [`enums.txt`](enums.txt) |
| `orchestration.v1.SourceMetadata` | `webpage_metadata` | 8 | singular | `WebpageMetadata`; exact-package closure + Dart symbol |
| `orchestration.v1.SourceMetadata` | `google_drive_source_metadata` | 10 | singular | `GoogleDriveSourceMetadata`; exact-package closure + Dart symbol |
| `orchestration.v1.Source` | `source_id` | 1 | singular | `SourceId`; exact-package closure + Dart symbol |
| `orchestration.v1.Source` | `title` | 2 | singular | string; exact-package closure + Dart symbol |
| `orchestration.v1.Source` | `metadata` | 3 | singular | `SourceMetadata`; exact-package closure + Dart symbol |
| `orchestration.v1.Source` | `settings` | 4 | singular | `.google.internal.labs.tailwind.v1.SourceSettings`; exact import origin |
| `tailwind.v1.SourceSettings` | `status` | 2 | singular | `SourceStatus`; `source_settings.pb.dart` + complete enum dump |
| `tailwind.v1.SourceSettings` | `user_drive_source_status` | 4 | singular | `UserDriveSourceStatus`; `source_settings.pb.dart` + complete enum dump |
| `orchestration.v1.ProjectMetadata` | `user_role` | 1 | singular | `ProjectRole`; complete enum dump |
| `orchestration.v1.ProjectMetadata` | `create_time` | 9 | singular | `.google.protobuf.Timestamp`; exact-package closure + Dart symbol |
| `orchestration.v1.Project` | `title` | 1 | singular | string; exact-package closure + Dart symbol |
| `orchestration.v1.Project` | `sources` | 2 | repeated | `Source`; exact-package closure + live response cardinality |
| `orchestration.v1.Project` | `id` | 3 | singular | string; exact-package closure + Dart symbol |
| `orchestration.v1.Project` | `emoji` | 4 | singular | string; exact-package closure + live response |
| `orchestration.v1.Project` | `metadata` | 6 | singular | `ProjectMetadata`; exact-package closure + Dart symbol |
| `orchestration.v1.GetProjectRequest` | `project_id` | 1 | singular | string; exact service closure + successful capture |
| `orchestration.v1.GetProjectRequest` | `include_audio_overview_ids` | 2 | singular | bool; exact service closure + successful capture |
| `orchestration.v1.GetProjectResponse` | `project` | 1 | singular | `Project`; exact wrapper declaration |
| `orchestration.v1.ListRecentlyViewedProjectsRequest` | `include_own_projects` | 2 | singular | bool; exact service closure + successful capture |
| `orchestration.v1.ListRecentlyViewedProjectsRequest` | `include_audio_overview_ids` | 3 | singular | bool; exact service closure + successful capture |
| `orchestration.v1.ListRecentlyViewedProjectsResponse` | `projects` | 1 | repeated | `Project`; exact wrapper declaration |

### B2 repository-local wire field ledger

Every row below is asserted against the generated descriptor and against deterministic serialized
bytes. Context fields observed in the official app are omitted because the direct bearer evidence
shows context is optional; the implementation does not fabricate one.

| Local message | Field | Tag | Cardinality | Type/evidence |
|---|---|---:|---|---|
| `WireCreateProjectRequest` | `name` | 1 | singular | string; captured create request |
| `WireDeleteProjectsRequest` | `project_ids` | 1 | repeated | string; captured single-ID delete request |
| `WireProjectChangeProperty` | `new_title` | 2 | singular | string; captured title-only mutation |
| `WireProjectMutation` | `change_property` | 4 | singular | local nested message; captured mutation variant |
| `WireMutateProjectRequest` | `project_id` | 1 | singular | string; captured mutation request |
| `WireMutateProjectRequest` | `mutations` | 2 | repeated | local nested message; captured cardinality |
| `WireCopyProjectRequest` | `source_project_id` | 2 | singular | string; direct successful replay |
| `WireCopyProjectRequest` | `title` | 3 | singular | string; direct successful replay |
| `WireGenerateNotebookGuideRequest` | `project_id` | 1 | singular | string; captured stateful request |
| `WireNotebookSummary` | `text_summary` | 1 | singular | string; captured guide response |
| `WireSuggestedTopic` | `question` | 1 | singular | string; captured guide topic row |
| `WireSuggestedTopic` | `prompt` | 2 | singular | string; captured guide topic row |
| `WireSuggestedTopics` | `topics` | 1 | repeated | local topic row; captured response cardinality |
| `WireNotebookGuide` | `summary` | 1 | singular | local summary message; captured response |
| `WireNotebookGuide` | `suggested_topics` | 2 | singular | local topic envelope; captured response |
| `WireGenerateNotebookGuideResponse` | `notebook_guide` | 1 | singular | local guide message; captured response wrapper |

## Blocked Project premium field 10

The flattened [`schema.proto`](schema.proto) names `Project.premiumFeatureInfo #10` and three bool
leaves, and the [web/Android audit](../notes/web-rpc-vs-mobile-grpc-audit-2026-08-07.md#123-lower)
observed a three-bool block on the web wire. Neither proves the exact protobuf package, FQN, or
import origin. The authoritative exact-package orchestration closure omits both
`PremiumFeatureInfo` and `Project #10`, and no independent descriptor or retained Dart-library
artifact is committed. B1 therefore declares neither symbol, pins tag #10 absent, and projects the
public `premium_features` field as `None`.

The same rule omits tier limits #11 and chat sessions #12. No unrecovered gaps or differently tagged
local-persistence `Project` duplicate enters the compile closure. A later work package may add a
small overlay only after committing independent exact-package descriptor/Dart-library evidence.

## B3 source-operation admission

The exact-package `supported.proto` snapshot above also supplies the orchestration message FQNs,
field tags/cardinality, import boundary, and service method manifest for `AddTentativeSources`,
`AddSources`, `DeleteSources`, `GenerateDocumentGuides`, and `LoadSource`. B3 copies only the fields
its builders and codecs reach into
`google/internal/labs/tailwind/orchestration/v1/b3_sources.proto`; it imports the B1 types rather
than redeclaring `Source` or `SourceId`. The overlay intentionally declares no service: the runtime
uses the evidence-qualified full method paths through `AndroidSession`'s generic typed callable,
so a second partial service descriptor cannot diverge from B1's checked service.

| Full method | Request FQN | Response FQN | Replay |
|---|---|---|---|
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/AddTentativeSources` | `.google.internal.labs.tailwind.orchestration.v1.AddTentativeSourcesRequest` | `.google.internal.labs.tailwind.orchestration.v1.AddTentativeSourcesResponse` | never |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/AddSources` | `.google.internal.labs.tailwind.orchestration.v1.AddSourcesRequest` | `.google.internal.labs.tailwind.orchestration.v1.AddSourcesResponse` | never |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/DeleteSources` | `.google.internal.labs.tailwind.orchestration.v1.DeleteSourcesRequest` | `.google.protobuf.Empty` | never |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GenerateDocumentGuides` | `.google.internal.labs.tailwind.orchestration.v1.GenerateDocumentGuidesRequest` | `.google.internal.labs.tailwind.orchestration.v1.GenerateDocumentGuidesResponse` | safe read |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/LoadSource` | `.google.internal.labs.tailwind.orchestration.v1.LoadSourceRequest` | `.google.internal.labs.tailwind.orchestration.v1.LoadSourceResponse` | safe read |

### B3 field ledger

| Package.Message | Fields admitted | Evidence/use |
|---|---|---|
| `orchestration.v1.InputSource` | `source_id #1` | exact closure; guide request/correlation |
| `orchestration.v1.Snippet` | `text_snippet #1` | exact closure; guide summary |
| `orchestration.v1.MainIdeas` | repeated `text_ideas #1` | exact closure; guide keywords |
| `orchestration.v1.DocumentGuide` | `source #1`, `snippet #2`, `main_ideas #3` | exact closure; exact-ID guide projection |
| `orchestration.v1.GenerateDocumentGuidesRequest/Response` | repeated `sources #1` / repeated `guides #1` | exact method closure |
| `orchestration.v1.TentativeSourceMetadata` | `name #1` | exact closure; bijective correlation key |
| `orchestration.v1.AddTentativeSourcesRequest` | repeated metadata `#1`, `project_id #2` | exact closure; B3 does not admit context/provenance |
| `orchestration.v1.AddTentativeSourcesResponse` | repeated `tentative_sources #1` | exact wrapper |
| `orchestration.v1.WebContent` | `url #1` | exact closure; outbound URL bytes |
| `orchestration.v1.UserContent` | `web_content #3`, `tentative_source_id #9` | exact closure; URL commit branch |
| `orchestration.v1.AddSourcesRequest/Response` | repeated `user_content #1`, `project_id #2` / repeated `sources #1` | exact method closure |
| `orchestration.v1.DeleteSourcesRequest` | repeated `source_ids #1` | exact method closure |
| `orchestration.v1.PlainTextSourceContent` | `header #1`, `body #2` | exact response closure; flat text uses body |
| `orchestration.v1.LoadSourceRequest/Response` | `source_id #1` / `source #1`, `plain_text #2`, `markdown_string #3` | exact method closure; TailwindDoc #4 remains unknown to the public document codec |

`WebContent.source_name #2`, the YouTube branch, text/Drive branches, freshness/refresh, and the
deep TailwindDoc grammar are omitted because B3 neither populates nor decodes them. Their presence
in the flattened recovery is not a reachability reason.

### Repository-local MutateSource overlay

The valid-resource replay in
[`web-parity-gap-live-validation-2026-08-27.md`](web-parity-gap-live-validation-2026-08-27.md#mutatesource)
(SHA-256 `c0a3a16b2ff0eba18395e5a53ae2ebddb3b299d8b2cae0d6d868a3e294b08251`)
proves the method path and request bytes — `SourceId #2`, repeated mutation `#3`, change-title
message `#1`, title `#1` — but does not prove a retained request protobuf FQN/import. B3 therefore
uses `notebooklm.internal.android.wire.MutateSourceWireRequest`, a visibly repository-local
wire-equivalent serializer, with the remote full method path and exact-package bare `Source`
response. No generated type falsely claims the remote request package.

## Deterministic toolchain

| Component | Exact value |
|---|---|
| `grpcio-tools` | `1.76.0` |
| embedded `protoc` | `libprotoc 31.1` |
| protobuf Python runtime | `6.31.1` |
| gRPC Python runtime | `1.76.0` |
| flags | both proto roots via `-I`, `--include_imports`, `--descriptor_set_out`, `--python_out`, `--grpc_python_out`; sorted input list |

Run `python scripts/regenerate_android_protos.py --check` in the locked dev environment. The check
compiles into a temporary directory, performs the repository-local Python import relocation, and
byte-compares the descriptor set and complete generated module tree. Use `--write` only when the
reviewed proto sources and pinned toolchain intentionally change.
