# Android protobuf evidence ledger

**Status:** admitted B1 read closure plus B2 notebook, B3/B3b source, B4 artifact, B5 chat,
B6 notes/sharing, and private B10 Research overlays

**Evidence snapshot:** 2026-08-29

**Scope:** B1 project/source reads, B2 notebook operations, the B3 URL/maintenance/flat-content
slice and B3b PDF transaction, B4 artifact list/get/create/update/delete plus its repository-local
wire-equivalent report-suggestion overlay, and the private/direct-test B5 chat surface
(`ListChatSessions`, `ListChatTurns`, `DeleteChatTurns`, and `GenerateFreeFormStreamed`), plus B6
note CRUD and public-link sharing, and the private B10 synchronous discovery plus async Research
lifecycle

## B10 Research method ledger

The service-free `research.proto` overlay copies the exact-package message and enum declarations
from the pinned `supported.proto`. The four async routes absent from the APK were independently
accepted by the Android bearer endpoint; `FinishDiscoverSourcesRun` and synchronous
`DiscoverSources` are also present in the committed mobile method manifest. The cumulative
`orchestration_service.proto` imports these messages and declares all six exact signatures; the
message overlay itself remains service-free because protobuf services cannot be reopened.

| Full method | Request / response | Replay policy |
| --- | --- | --- |
| `.../DiscoverSources` | `DiscoverSourcesRequest` / `DiscoverSourcesResponse` | never; quota-bearing discovery |
| `.../DiscoverSourcesManifold` | `DiscoverSourcesManifoldRequest` / `DiscoverSourcesManifoldResponse` | never; stateful start |
| `.../DiscoverSourcesAsync` | `DiscoverSourcesAsyncRequest` / `DiscoverSourcesAsyncResponse` | never; stateful start |
| `.../ListDiscoverSourcesJob` | `ListDiscoverSourcesJobRequest` / `ListDiscoverSourcesJobResponse` | replay-safe read; exact run ID selected locally |
| `.../CancelDiscoverSourcesJob` | `CancelDiscoverSourcesJobRequest` / zero-byte `google.protobuf.Empty` | never; ambiguity resolved only by exact-ID poll |
| `.../FinishDiscoverSourcesRun` | `FinishDiscoverSourcesRunRequest` / `FinishDiscoverSourcesRunResponse` | never; URL-only missing subset may be reconciled and resent |

Fast start uses `ResearchQuery #1`, mode `#3=1`, project `#4`, and canonical run UUID response
`#1`. Deep uses packed flags `#2=[1]`, query `#3`, mode `#4=5`, project `#5`; response `#1` is
diagnostic and canonical run UUID `#2` owns poll/cancel/import. Poll jobs carry canonical ID `#1`,
info `#2`, update/create timestamps `#3/#4`; info carries query `#2`, mode `#3`, results `#4`, and
status `#5`. URL result fields are URL/title/hint/corpus/content/ordinal `#1/#2/#3/#4/#7/#9`.
A report is URL-less with Markdown `ResearchResultContent.text #1`, kind `#2=3`. Finish encodes
URL rows as `UserContent.web_content #3`; report rows use `text_content #2` and
`text_content_type #4=MARKDOWN(3)`. Response headers are repeated `#1` and may be omitted.

This ledger is the admission boundary for `src/notebooklm/_android/proto_src/`. The recovered
[`schema.proto`](schema.proto) is Dart-AOT evidence, not a compile input: it flattened several
libraries and contains duplicate package-local persistence declarations. The checked-in B1 proto
sources instead preserve the exact wire packages from the descriptor/Dart library boundary and
copy only the fields below. Missing fields remain protobuf unknown fields; they are not filled from
plausible-looking flattened declarations. B5 follows the same rule: its exact-package message
overlay admits only fields retained by the named Dart protobuf libraries and checked against
captured wire tags. One cumulative `orchestration_service.proto` now owns the exact service and
imports only message signatures independently admitted below; repository-local or otherwise
unproven signatures remain manual full-path calls listed in the machine-readable
[`grpc-service-signature-exceptions.json`](grpc-service-signature-exceptions.json). B6 sharing is
the one explicit exception to an exact
package overlay: its field bytes are proven, but imported response/empty-message FQNs are not.
Those messages therefore live under the visibly repository-local `notebooklm.android.wire.v1`
package and make no claim about Google's type identity.

## Evidence input identities

The external exact-package snapshot was reviewed, reduced to the admitted fields, and then made
self-contained by this ledger, the checked-in proto sources, descriptor set, and synthetic
fixtures. Hashes prevent a later local checkout from silently changing what was admitted.

| Evidence input | SHA-256 | Role |
|---|---|---|
| exact-package orchestration `supported.proto` | `829c4ee871fd66421ee098fa266793ec68773e625ff005cc519b2c0f7c191ae9` | service/message FQNs, package, tags, cardinality, import origin |
| exact-package `source_settings.proto` | `becd695c4281e23064c16fc1441c61117e5dc2a44c52cadf44af9e31c7cb8b18` | separate settings package, fields #2/#4, complete enums |
| [`schema.proto`](schema.proto) | `aa9f49d302ff9a64cc16d08b2f2f9031f77a348b3707dd98df37be91a67355ec` | flattened Dart recovery used to identify gaps, never as a compile input; hash includes the curated `docs/android/` path comments |
| [`enums.txt`](enums.txt) | `8c8137c1842d07b54ba9e52feeea7c3ce09246415c26d964d17bec68eee228bc` | exhaustive enum names and integers |
| exact method manifest | `c2cf4bf2e6cdefd35232f01572070fbe07d11ef9bad99b556f76b5e3748f38a3` | full method paths, request/response FQNs, unary cardinality |
| [`file-transfer-live-validation-2026-08-27.md`](file-transfer-live-validation-2026-08-27.md) | `c713a7cfe5058482aa8fc9a0201ad08487296700223f23829842795f85713107` | official-app/headless PDF upload request and live artifact representation/direct infographic PNG transfer |
| [`web-parity-gap-live-validation-2026-08-27.md`](web-parity-gap-live-validation-2026-08-27.md) | `c0a3a16b2ff0eba18395e5a53ae2ebddb3b299d8b2cae0d6d868a3e294b08251` | live delete, rename/read-back, report-suggestion cardinality, and disposable note CRUD |
| [`notebooks-live-validation-2026-08-28.md`](notebooks-live-validation-2026-08-28.md) | `a6646ea8f96c3c9c63aafc4d3d30d2fde8276c67155cf60bd885cd42a48a4036` | accepted Android bearer plus disposable emoji set/clear/combined read-back and repeated Recent failure |
| [`notes-mind-maps-live-validation-2026-08-28.md`](notes-mind-maps-live-validation-2026-08-28.md) | `53510a6cc807dcc8f1f652039e667190763c5c08a4debe64bc52bbf8bf8825f3` | two same-id Web-generation/Android-read classifier runs, kind-safe Android map deletion, and two complete-manifest reruns retaining the cross-backend ordinary-note tombstone boundary |
| [`endpoints.md`](endpoints.md) | `57467b424515cf0dfa4c3e08c636ab6a8d0bfddbe5701cf50675ea39338e4e62` | live request/response envelopes, route results, and captured note/sharing bytes |

The recovery method and the warning about duplicate packages are committed in
[`README.md`](README.md#caveats-that-will-bite-you). Live request/response shapes are documented in
[`endpoints.md`](endpoints.md#getproject).

## Cumulative generated service ledger

| Full method | Request FQN | Response FQN | Cardinality | Request fields populated by B1 |
|---|---|---|---|---|
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GetProject` | `.google.internal.labs.tailwind.orchestration.v1.GetProjectRequest` | `.google.internal.labs.tailwind.orchestration.v1.GetProjectResponse` | unary/unary | `project_id #1`, `include_audio_overview_ids #2`; no `RequestContext` |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/ListRecentlyViewedProjects` | `.google.internal.labs.tailwind.orchestration.v1.ListRecentlyViewedProjectsRequest` | `.google.internal.labs.tailwind.orchestration.v1.ListRecentlyViewedProjectsResponse` | unary/unary | `include_own_projects #2`, `include_audio_overview_ids #3`; no `RequestContext` |

The two B1 signatures above and twenty-two later exact signatures live in the sole
`google/internal/labs/tailwind/orchestration/v1/orchestration_service.proto` service declaration.
The individual message overlays remain service-free so protobuf never reopens one service across
files. Its generated stub exposes 24 implemented methods: the two reads above, five source methods,
five artifact methods, three chat methods, three note methods, and six Research methods. Eleven
other implemented paths
remain manual full-path calls because at least one remote request/response FQN is unproven. The
exception manifest names each adapter constant, local parser, reason code, and evidence link;
descriptor/adapter/manifest equality is pinned by
`tests/unit/android/test_grpc_service_manifest.py`.

B2 does not enter the exact service descriptor: its captures prove method paths and serialized
layouts, but not the complete package/import identity of every reachable request/response message.
B2 therefore uses the repository-local wire overlay below with manual full-path calls.

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
`artifacts.proto` was independently checked against the exact-package archived
`supported.proto` whose SHA-256 is pinned above. References to the flattened `schema.proto` in
source comments are corroborating Dart-symbol evidence, never the authority for a Google FQN.
The B4 message overlay intentionally declares no second protobuf `service`: protobuf cannot reopen
the same service across files. The cumulative exact service imports its five implemented exact
signatures. `GenerateReportSuggestions` remains an explicit manifest exception because its
repository-local `*Wire` types make no Google FQN claim. `AndroidSession` continues to dispatch
paths generically with the ledgered message classes.

| Full method | Exact request FQN | Exact response FQN | B4 disposition |
|---|---|---|---|
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/ListArtifacts` | `.google.internal.labs.tailwind.orchestration.v1.ListArtifactsRequest` | `.google.internal.labs.tailwind.orchestration.v1.ListArtifactsResponse` | admitted safe aggregate Studio read |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GetArtifact` | `.google.internal.labs.tailwind.orchestration.v1.GetArtifactRequest` | `.google.internal.labs.tailwind.orchestration.v1.GetArtifactResponse` | admitted safe single-artifact polling read; common SDK `get` remains concrete over aggregate `list` so note-backed mind maps remain visible |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/CreateArtifact` | `.google.internal.labs.tailwind.orchestration.v1.CreateArtifactRequest` | `.google.internal.labs.tailwind.orchestration.v1.CreateArtifactResponse` | evidence-qualified quiz and Audio Overview mutations, never replayed |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/UpdateArtifact` | `.google.internal.labs.tailwind.orchestration.v1.UpdateArtifactRequest` | `.google.internal.labs.tailwind.orchestration.v1.Artifact` | title-only mutation with etag, never replayed, then list read-back |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/DeleteArtifact` | `.google.internal.labs.tailwind.orchestration.v1.DeleteArtifactRequest` | `.google.protobuf.Empty` | never replayed; sanitized `NOT_FOUND` is idempotent success |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GenerateReportSuggestions` | APK-absent; repository-local `GenerateReportSuggestionsRequestWire` | APK-absent; repository-local `GenerateReportSuggestionsResponseWire` | safe live-added read; no Google message-FQN claim |

## B4 exact artifact field ledger

This table is exhaustive for `artifacts.proto`; fields present in the archived message but not
needed by B4 are deliberately left unknown. “Exact” means the pinned exact-package archive, not a
field inferred from its plausible Dart name.

| Exact-package message | Admitted fields (`name #tag`, cardinality and type) | B4 use |
|---|---|---|
| `MediaStreamingUrl` | `url #1` string; `type #2` `MediaStreamingType` | audio/video representation projection |
| `QuizGenerationOptions` | `question_quantity #1` `QuestionQuantity`; `quiz_difficulty #2` `QuizDifficulty` | quiz create options |
| `AppArtifactGenerationOptions` | `app_type #1` `AppType`; `free_text_steering_prompt #3` string; `quiz_generation_options #8` message | quiz kind/prompt/create |
| `AppArtifact` | `generation_options #2` message | app kind/options; HTML/content fields remain unknown and unsupported |
| `EpisodeLength` | exhaustive values: unspecified `0`, short `1`, medium `2`, long `3` | Audio Overview creation length |
| `AudioOverviewGenerationOptions` | `episode_focus #1` string; `episode_length #2` `EpisodeLength`; repeated `source_ids #4` imported exact `SourceId`; `language_code #5` string | Audio Overview create and existing prompt/source projections |
| `AudioOverviewArtifact` | `generation_options #2` message; `is_interactive #5` bool; repeated `media_urls #6`; `duration #7` `google.protobuf.Duration` | create/list/poll projection; `is_interactive` is not projected because public `Artifact` has no corresponding field |
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
| `ArtifactSource` | `source_id #1` imported exact `SourceId` | source IDs and quiz/audio requests |
| `Artifact` | `artifact_id #1` string; `title #2` string; `type #3` `ArtifactType`; repeated `sources #4`; `status #5` `ArtifactStatus`; `audio_overview #7`; `tailored_report #8`; `explainer_video #9`; `app #10`; `last_modified_timestamp #11` Timestamp; `infographic #15`; `slides #17`; `etag #22` string; `file #25` | the complete B4 public projection and mutation subset |
| `CreateArtifactRequest` / `Response` | request `project_id #2`, `artifact #3`; response `artifact #1` | quiz and Audio Overview mutations |
| `GetArtifactRequest` / `Response` | request `artifact_id #1`; response `artifact #1` | exact single-artifact polling primitive; not substituted for aggregate public `get` |
| `ListArtifactsRequest` / `Response` | request `project_id #2`; response repeated `artifacts #1` | Studio aggregate listing |
| `UpdateArtifactRequest` | `artifact #1`; `update_mask #2` FieldMask; `etag #3` string | title rename |
| `DeleteArtifactRequest` | `artifact_id #2` string | idempotent delete |

The five exact top-level enums are also pinned exhaustively by generated descriptor tests:
`ArtifactType` 0–10, `ArtifactStatus` 0–6, `AppType` 0–5, `EpisodeLength` 0–3, and
`MediaStreamingType` 0–4. The two nested quiz enums are 0–3. Unknown future integers remain unknown
rather than being coerced to a known family.

### B4 quiz request

The successful quiz branch sends `CreateArtifactRequest.project_id #2` and `artifact #3`, with
`Artifact.type = ARTIFACT_TYPE_APP`, repeated `Artifact.sources #4`, and
`AppArtifact.generation_options.app_type = APP_TYPE_QUIZ`. Quantity/difficulty use the exact nested
enums at quiz option fields #1/#2; free text uses app option field #3. No other family builder is
admitted in the B4 quiz slice.

### B4 Audio Overview request

The captured Audio Overview branch uses the same exact `CreateArtifact` method with
`Artifact.type = ARTIFACT_TYPE_AUDIO_OVERVIEW`. It requires at least one source ID and a non-empty
language code. Every ordered source ID is duplicated in top-level `Artifact.sources #4` and nested
`AudioOverviewGenerationOptions.source_ids #4`; focus is field `#1`, `EpisodeLength` is field `#2`,
and language is field `#5`. Public `AudioLength.SHORT`, `DEFAULT`, and `LONG` map exactly to enum
values `1`, `2`, and `3`; omitted length sends medium/default `2`. The captured exact closure has no
audio-format field, so only omitted format or `AudioFormat.DEEP_DIVE` is accepted. BRIEF, CRITIQUE,
and DEBATE reject before source resolution or transport I/O. Creation is non-replay-safe and source
resolution plus mutation share one lifecycle operation/epoch lease.

The exact `GetArtifact` request carries only `artifact_id #1` in the admitted closure and is used
for one safe polling read per tick. The public aggregate `get`/`get_or_none` methods deliberately
remain list-based so their established note-backed mind-map semantics are unchanged.

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

### B5 method ledger

B5's message overlay remains service-free because protobuf services cannot be extended across
source files. The cumulative service imports the exact `ListChatSessions`, `ListChatTurns`, and
`GenerateFreeFormStreamed` signatures. `DeleteChatTurns` returned zero bytes, so B5 deserializes it
with the wire-equivalent `google.protobuf.Empty` implementation without claiming that WKT as the
remote response FQN; that path remains an explicit manifest exception.

| Full method | Request overlay | Response deserializer | Cardinality | Retry/telemetry contract |
|---|---|---|---|---|
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/ListChatSessions` | `.google.internal.labs.tailwind.orchestration.v1.ListChatSessionsRequest` | `.google.internal.labs.tailwind.orchestration.v1.ListChatSessionsResponse` | unary/unary | replay-safe |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/ListChatTurns` | `.google.internal.labs.tailwind.orchestration.v1.ListChatTurnsRequest` | `.google.internal.labs.tailwind.orchestration.v1.ListChatTurnsResponse` | unary/unary | replay-safe |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/DeleteChatTurns` | `.google.internal.labs.tailwind.orchestration.v1.DeleteChatTurnsRequest` | zero-byte wire-equivalent `google.protobuf.Empty` | unary/unary | non-replay-safe |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GenerateFreeFormStreamed` | `.google.internal.labs.tailwind.orchestration.v1.GenerateFreeFormStreamedRequest` | `.google.internal.labs.tailwind.orchestration.v1.GenerateFreeFormStreamedResponse` | unary/server-streaming | no retry; one aggregate deadline; `telemetry_method=None` |

## B6 service ledger

The full method paths below come from the exact method inventory and were routed live. Note CRUD
also has valid-resource semantic proof on a disposable copied notebook: create and mutate read back
the exact title/content, and delete was eventually visible (the first read could retain the row;
the next excluded it). Request context fields were optional in that successful replay and remain
omitted. The cumulative orchestration service admits `GetNotes`, `CreateNote`, and `MutateNote`.
`DeleteNotes` remains an exception because its zero-byte response protobuf FQN is unproven. Both
sharing paths remain exceptions because their field-proven parsers intentionally use a
repository-local package rather than claiming the remote message FQNs; no sharing service is
fabricated.

| Full method | Request/response evidence | Replay policy | B6 projection |
|---|---|---|---|
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GetNotes` | exact-package `GetNotesRequest` / `GetNotesResponse`; two same-ID cross-backend live runs | safe read | ordinary notes exclude prompt-typed and JSON-shaped maps; private minimal map rows expose exact `[id, content]` only and do not claim full Web raw-row parity |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/CreateNote` | exact-package `CreateNoteRequest` / `CreateNoteResponse` | never replay | create, then exact read-back |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/MutateNote` | exact-package `MutateNoteRequest` / `MutateNoteResponse` | never replay | existence preflight, edit, exact read-back |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/DeleteNotes` | exact request; zero-byte success response parsed by local `EmptyResponse`; live map deletion preserved an ordinary sibling and a second delete succeeded | never replay | kind-safe idempotent preflight plus bounded eventual-absence reads for notes and note-backed maps |
| `/labs.language.tailwind.sharing.LabsTailwindSharingService/GetProjectDetails` | exact method/message names; local wire-equivalent fields | safe read | public settings #2, cap #3, policy #4 only |
| `/labs.language.tailwind.sharing.LabsTailwindSharingService/ShareProject` | exact method/message names; local wire-equivalent fields; zero-byte success | never replay | public readability, then `GetProjectDetails` |

`GetNotes` status 5 maps to `NotebookNotFoundError`; a missing listed note maps to
`NoteNotFoundError`. Create/share status 5 maps to the notebook miss. Mutate status 5 maps to the
note miss after its existence preflight. A delete status 5 after a successful preflight is the
idempotent concurrent-absence outcome.

Each of the eight manifest-shaped callables on the private Android Notes adapter owns exactly one
supervisor operation scope.
Create/update read-back and both delete preflight/poll workflows pass that scope's `expected_epoch`
to every unary call instead of composing through another public method. The private typed
note-backed map read used by B7 is scoped independently as well. Graceful drain therefore admits
the already-started workflow through its final read, cancellation settles the complete operation,
and a forced close/reopen rejects any old-epoch read-back or poll before it can touch the new
transport generation.

## Import closure

| Import | Exact package | Why reachable |
|---|---|---|
| `google/internal/labs/tailwind/v1/source_settings.proto` | `google.internal.labs.tailwind.v1` | `Source.settings #4`; Dart library boundary is `source_settings.pb.dart` |
| `google/protobuf/timestamp.proto` | `google.protobuf` | `ProjectMetadata.create_time #9` |

No common, agency, artifact, chat, mutation, context, Empty, Struct, Duration, or FieldMask proto is
reachable from the B1 read projection.

B6 adds the exact `google.protobuf.Timestamp`-typed `last_edit_timestamp` note field but deliberately
does not project it as public `Note.created_at`: a last edit is not evidence of creation time. The
sharing and zero-byte response types need no external imports because their overlays are local.

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
| `WireProjectChangeProperty` | `new_emoji` | 3 | optional | string; repository-local name, live set/clear/combined mutation plus bare-response and `GetProject` read-back |
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

### B5 field ledger

The compile inputs are
[`chat.proto`](../../src/notebooklm/_android/proto_src/google/internal/labs/tailwind/orchestration/v1/chat.proto)
(SHA-256 `a4d1a93ccb15ecc8f7328b3a9ede68e71ac4921401cf023fe729285b798b887f`) and
[`chat_history.proto`](../../src/notebooklm/_android/proto_src/labs/language/tailwind/common/protos/chat_history.proto)
(SHA-256 `7e8551fe837ac30f80d3d5f5d07f33c1c1dd24970b33c558c86b4dba799d9bb8`).
The shared `InputSource` declaration is imported from
[`sources.proto`](../../src/notebooklm/_android/proto_src/google/internal/labs/tailwind/orchestration/v1/sources.proto)
rather than redeclared in the B5 overlay.
`tests/unit/android/test_chat_proto_contract.py` asserts the following list exhaustively against
the generated descriptors; no undeclared semantic leaf is available to the adapter.

| Package.Message | Admitted fields (tag; cardinality/type) |
|---|---|
| `common.protos.ChatSession` | `chat_session_id` (1; string) |
| `orchestration.v1.InputSource` | `source_id` (1; `SourceId`) |
| `orchestration.v1.ConversationEvent` | `text` (1; string), `type` (3; captured nested enum) |
| `orchestration.v1.ConversationTurnKey` | `session_id` (1; string), `conversation_id` (2; string), `observed_field_3` (3; opaque int32) |
| `orchestration.v1.ListChatSessionsRequest/Response` | `project_id` (3; string); `sessions` (1; repeated `ChatSession`) |
| `orchestration.v1.ListChatTurnsRequest/Response` | `chat_session_id` (4; string), `page_token` (6; string); `chat_turns` (1; repeated `ChatHistoryMessage`), `next_page_token` (2; string) |
| `orchestration.v1.ChatHistoryMessage` | `message_id` (1; string), `timestamp` (2; `Timestamp`), `observed_event_type` (3; raw int32 role), `user_query_text` (4; string), `act_on_sources_response` (5; `ActOnSourcesResponse`) |
| `orchestration.v1.ActOnSourcesResponse` | `response` (1; `AnswerResponse`) |
| `orchestration.v1.DeleteChatTurnsRequest` | `chat_session_id` (2; string), `delete_all_history` (4; bool) |
| `orchestration.v1.GenerateFreeFormStreamedRequest` | `sources` (1; repeated `InputSource`), `user_query` (2; string), `conversation_history` (3; repeated `ConversationEvent`), `chat_session_id` (5; string), `user_message_id` (6; string), `project_id` (8; string), `origin` (9; `QueryOrigin`) |
| `orchestration.v1.GenerateFreeFormStreamedResponse` | `answer` (1; `AnswerResponse`), `is_final_response` (5; bool) |
| `orchestration.v1.AnswerResponse` | `response` (1; string), `conversation_turn_key` (3; `ConversationTurnKey`), `response_doc` (5; `TailwindDoc`) |
| `orchestration.v1.TailwindDoc` | `body` (1; `Body`), `objects` (4; repeated `DocumentObject`) |
| `orchestration.v1.Body` | `content` (1; repeated `StructuralElement`), `inline_object_locations` (2; repeated `AnnotationMapEntry`) |
| `orchestration.v1.StructuralElement` | `start_index` (1; int32), `end_index` (2; int32), `paragraph` (3; `Paragraph`) |
| `orchestration.v1.Paragraph` / `ParagraphElement` / `TextRun` | `elements` (1; repeated); `start_index` (1), `end_index` (2), `text_run` (3); `content` (1) |
| `orchestration.v1.AnnotationMapEntry` / `ObjectId` / `Range` | `object_id` (1), `content_range` (2); `id` (1); `start_index` (2), `end_index` (3) |
| `orchestration.v1.DocumentObject` / `Citation` | `object_id` (1), `citation` (2); `fragment` (5), `source_attribution` (6), `object_id` (7) |
| `orchestration.v1.TailwindDocFragment` / `CitationSource` / `SourceRevision` | `elements` (1; repeated `StructuralElement`); `ingested_source` (1); `source` (1; `SourceId`) |

The request origin and conversation-event enums come from the exhaustive checked-in
[`enums.txt`](enums.txt). Cached turns are mapped in the captured newest-first event order for each
turn: generated response (`2`) followed by user query (`1`). Every ask supplies a caller-generated
`user_message_id`; no server turn identifier is guessed. Prior-turn counting returns each
`ChatHistoryMessage.observed_event_type` unchanged to the neutral base, which counts only raw role
value `1` as a question; Android does not replace every row with a synthetic user role.

Stream responses are cumulative snapshots. The adapter retains the latest frame whose response
field 5 is true, never concatenates frames, and raises `ChatResponseParseError` when EOF arrives
without that final marker. The final answer's `responseDoc` is the only citation source: source
identity descends
through `DocumentObject.citation.source_attribution.ingested_source.source`, cited text comes only
from the proven fragment paragraph fields, and answer anchors join through the proven body
annotation/object IDs. Unrecovered citation score/range fields are not declared or projected.
Citation numbering follows the raw one-based `TailwindDoc.objects` ordinal, so a preceding
non-citation object intentionally leaves a numbering gap instead of renumbering later citations.

The synthetic wire fixture
[`chat_wire.json`](../../tests/fixtures/android/chat_wire.json) (SHA-256
`674f05b27f5bfd92baac39833fd5769a91c4d85962983e88b47a354494ec52bf`) pins a request,
partial/final cumulative frames, history, and sessions at serialized-byte level. The generated
descriptors are part of the canonical cumulative
[`android_descriptor_set.pb`](../../tests/fixtures/android/android_descriptor_set.pb) fixture and
are byte-checked by the deterministic regeneration command below.

### B5 evidence-gated omissions

- `RequestContext` and `Provenance` are not populated; B5 does not invent their capture-specific
  semantic values.
- Chat configure/settings remain unsupported and fail before transport I/O. `set_mode` deliberately
  reaches that same configure rejection.
- Saved-from-chat note creation delegates to B6's exact `CreateNote` sender with the captured
  `SAVED_RESPONSE` note type; no additional citation payload fields are invented.
- Document tables, styles, non-paragraph structural variants, speculative citation score/range
  slots, and next-step suggestions are left as protobuf unknown fields.
- `ListChatTurns` returns its raw first response page. The public-shaped history decoder applies its
  caller limit and reverses the captured newest-first rows; it does not guess pagination policy from
  the presence of `next_page_token`.

## B6 field ledger

The exact-package note overlay declares only these recovered fields:

| Package.Message | Field | Tag | Cardinality | Type/evidence |
|---|---|---:|---|---|
| `orchestration.v1.NoteMetadata` | `type` | 1 | singular | `NoteType`; exhaustive enum dump |
| `orchestration.v1.NoteMetadata` | `last_edit_timestamp` | 3 | singular | `google.protobuf.Timestamp`; recovered Dart symbol |
| `orchestration.v1.NoteMetadata` | `note_prompt_type` | 4 | singular | `NotePromptType`; exhaustive enum dump |
| `orchestration.v1.ProjectNote` | `id` | 1 | singular | string |
| `orchestration.v1.ProjectNote` | `content` | 2 | singular | string |
| `orchestration.v1.ProjectNote` | `metadata` | 3 | singular | `NoteMetadata` |
| `orchestration.v1.ProjectNote` | `name` | 5 | singular | string |
| `orchestration.v1.NoteOrStatus` | `note` | 2 | singular | `ProjectNote`; status arm #1 unrecovered/unknown |
| `orchestration.v1.GetNotesRequest` | `project_id` | 1 | singular | string; context #4 omitted |
| `orchestration.v1.GetNotesResponse` | `notes` | 1 | repeated | `NoteOrStatus` |
| `orchestration.v1.CreateNoteRequest` | `project_id`, `content`, `metadata`, `name` | 1, 2, 3, 5 | singular | exact tags; context #7 omitted |
| `orchestration.v1.CreateNoteResponse` | `note` | 1 | singular | `ProjectNote` |
| `orchestration.v1.NoteMutation_EditNoteMutation` | `content`, `name` | 1, 2 | singular | exact edit payload |
| `orchestration.v1.NoteMutation` | `edit_note_mutation` | 1 | singular | edit mutation |
| `orchestration.v1.MutateNoteRequest` | `project_id`, `note_id`, `mutations` | 1, 2, 3 | singular, singular, repeated | context #4 omitted |
| `orchestration.v1.MutateNoteResponse` | `note` | 1 | singular | `ProjectNote` |
| `orchestration.v1.DeleteNotesRequest` | `project_id`, `note_ids` | 1, 3 | singular, repeated | context #4 omitted |

The repository-local sharing overlay admits only these bytes:

| Local wire message | Field | Tag | Cardinality | Evidence boundary |
|---|---|---:|---|---|
| `ProjectPublicSettings` | `is_publicly_readable`, `is_discoverable` | 1, 2 | singular | recovered common message + live response/request |
| `GetProjectDetailsRequest` | `project_id` | 1 | singular | successful capture; context #2 omitted |
| `GetProjectDetailsResponse` | `public_settings` | 2 | singular | recovered field; tag #1 not decoded |
| `GetProjectDetailsResponse` | `max_individuals_share_limit` | 3 | optional | recovered field; presence preserved |
| `GetProjectDetailsResponse` | `is_public_sharing_allowed` | 4 | optional | recovered field; false remains distinct from absent |
| `ShareProjectRequest_ProjectToShare` | `project_id`, `public_document_settings` | 1, 3 | singular | successful public/private mutation shape |
| `ShareProjectRequest_PublicDocumentSettings` | `is_publicly_readable`, `is_discoverable` | 1, 2 | singular | successful capture |
| `ShareProjectRequest` | `project` | 1 | repeated | successful capture; context #4 omitted |

Collaborator/owner response tag #1 is absent from the recovered mobile descriptor, so Android
returns `shared_users=[]`. Populated but unnamed response tags #7/#8 remain protobuf unknown fields.
No collaborator mutation, view-level mutation, or Android settings API is admitted.

## Cross-backend mind-map classifier and the B5 seam

The exact prompt enum remains one sufficient map-kind signal, but it is not necessary. Two sanitized
live runs generated a note-backed map through Web and read the same id over Android. Both Android
rows were `NoteType.USER_WRITTEN`, `NotePromptType.NOTE_PROMPT_TYPE_UNSPECIFIED`, and a JSON object
with a top-level `children` key. Android therefore uses the union of the exact prompt signal and the
legacy Web JSON-object signals: `MIND_MAP`, or parsed object membership of `children` or `nodes`.
Ordinary note listing excludes that same union.

The private Android adapter projects only `[ProjectNote.id, ProjectNote.content]`, the two fields
proved by same-ID capture. The established public Web method returns raw rows with additional
metadata/source slots. No exact Android capture proves those slots or a wire-equivalent replacement,
so the minimal projection is useful for direct conformance and kind-safe deletion but does not pass
the full substitution gate. `delete_mind_map` preflights this kind-specific list, sends one
non-replayed `DeleteNotes`, and polls bounded `GetNotes` reads until that map id is absent. An ordinary
note id and an already-absent map are read-only idempotent successes. The retained capture narrative is
[`notes-mind-maps-live-validation-2026-08-28.md`](notes-mind-maps-live-validation-2026-08-28.md).

The second promotion blocker is the public `Note.created_at` contract. B6 has exact
`last_edit_timestamp` evidence only; it deliberately does not copy that value into creation time.
The third is deletion read-back: Android exact-ID lookup reports absence after `DeleteNotes`, while
Web exact-ID lookup exposes the same persisted soft-delete id with empty title/content. Both ordinary
lists exclude it, but the established `get_or_none` results differ. Until exact evidence resolves all
three gaps, normal client assembly must keep the Notes namespace on Web.

The exact `CreateNote` builder and sender are reusable by the B5 `AndroidChatAPI` private save-note
hook with `note_type=SAVED_RESPONSE_NOTE_TYPE`; B6 does not duplicate or fabricate the chat
adapter.

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
`google/internal/labs/tailwind/orchestration/v1/sources.proto`; it imports the B1 types rather
than redeclaring `Source` or `SourceId`. The message overlay intentionally declares no service. The
cumulative service imports the five exact signatures below, while the repository-local
`MutateSource` request remains an explicit manifest exception. Runtime dispatch stays on
`AndroidSession`'s generic typed callable.

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
| `orchestration.v1.AddTentativeSourcesRequest` | repeated metadata `#1`, `project_id #2`, `request_context #3`, `provenance #4` | exact closure; B3 URL builders leave #3/#4 absent, B3b PDF registration populates them |
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

## B3b exact-package PDF upload admission

The upload closure does not infer packages from the flattened message list. The independently
recovered Dart library boundaries in [`schema.proto`](schema.proto) name
`google.internal.labs.tailwind.orchestration.v1/labs_tailwind_orchestration_service.pb.dart`,
`labs.language.tailwind.common.protos/metadata.pb.dart`, and
`labs.language.tailwind.common.protos/provenance.pb.dart`. Those package/library identities,
together with the exhaustive nested enum inventory in [`enums.txt`](enums.txt), admit the exact
FQNs below. The live report then proves that every declared field is reachable in the successful
PDF request. No capability, execution-mode, app-API, or unused provenance field is copied.

| Package.Message | Fields/enums admitted | Evidence/use |
|---|---|---|
| `common.protos.ClientInfo` | nested `ApplicationPlatform { UNSPECIFIED=0, NATIVE=2 }`; nested `Device { UNSPECIFIED=0, MOBILE_ANDROID=1 }`; `application_platform #1`, `device #2`, `application_version #3` | exact Dart library boundary + exhaustive enum dump; both registration and start JSON provenance |
| `common.protos.Provenance` | nested `OriginProductType { UNSPECIFIED=0, GOOGLE_NOTEBOOKLM=1 }`; `origin_product_type #1`, `client_info #11` | exact Dart library boundary + successful live body |
| `common.protos.ClientType` | `UNKNOWN=0`, `ANDROID_APP=3` | exact enum inventory; registration context |
| `common.protos.ClientMetadata` | `client_version #1` | exact Dart library boundary; captured app version |
| `common.protos.RequestContext` | `client_type #1`, `client_metadata #2`, `provenance #4` | exact Dart library boundary; unreachable fields #3/#5/#6 omitted |
| `orchestration.v1.UploadFileRequest` | `project_id #3`, `request_context #4`, `source_id #5`, `provenance #6` | exact orchestration Dart library boundary + structurally matched successful start JSON |

`UploadFileRequest` is used as a deterministic binary descriptor/field-number gate. Runtime JSON
is an explicit captured-field builder, not a generic protobuf-to-dictionary layer. The only B3b
registration route remains the already admitted `AddTentativeSources` unary method, always
non-replayed. Scotty start/finalize are HTTP and add no guessed gRPC service declarations.

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
compiles the cumulative B1-B6 message and exact-service closure into a temporary directory,
performs the repository-local Python import relocation for every exact package root, and
byte-compares the canonical descriptor set plus the complete generated module tree. Use `--write`
only when the reviewed proto sources and pinned toolchain intentionally change.
`notes_sharing_request_wires.json` independently pins every populated
B6 request byte sequence.
