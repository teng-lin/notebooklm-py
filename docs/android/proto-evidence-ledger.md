# Android protobuf evidence ledger

**Status:** admitted read, notebook, source/upload, artifact, chat, notes/sharing,
organization, Research, and public account-settings contracts

**Evidence snapshot:** 2026-08-31 (`GenerateDocumentGuides` echo and derived-read existence policing re-probed live)

**Scope:** project/source reads; notebook operations; URL, maintenance, content, and generic file
source operations; artifact list/get/create/derive/update/delete, native note-backed mind-map
generation, the generated web-derived report-suggestion closure, and exact representation payloads;
chat sessions, turns, deletion, and streaming; note CRUD plus public-link and collaborator sharing;
label and collection CRUD/membership; synchronous and asynchronous Research; and native account
language/limit settings

## Account method ledger

The exact-package `account.proto` overlay retains the semantic bootstrap subset from the pinned
`supported.proto`: `UserInfo.accepted_tos #1`, `opted_in_to_marketing_emails #4`, and
`is_eea_user #9`; `PremiumUserInfo.is_premium_user #1`; and `Account.user_info #3` plus
`premium_user_info #5`. The official APK additionally proves the exact `MutateAccount` method and
the outer mutation closure. Current authenticated Web constructors and live Android calls admit the
settings fields: `OutputLanguage.language_code #1`, `UserInfo.output_language #5`,
`Account.tier_limits #2`, and optional `TierLimits` integers `#1` through `#5`. Fields `#1` through
`#4` are also decoded by the current Web account mapper; field `#5` is independently
positional-recorded and live-present. The conventional local name `account_type` for tier field
`#1` remains semantically opaque.

`GetOrCreateAccountRequest.request_context #1` is current-bundle builder-derived and live accepted;
its response remains `account #1`. `MutateAccountRequest` carries repeated `AccountMutation #1`
and `RequestContext #2`; the mutation oneof uses `change_property #2`, whose nested
`new_user_info #1` carries the replacement output language. The RPC returns bare `Account`. Both
methods are non-replayed: bootstrap may create state and language mutation is stateful.

The private `AndroidAccountAPI` still exposes the strict four-boolean bootstrap projection. The
public `AndroidSettingsAPI` uses the same exact account closure for all four settings methods:
language read/write and account/user-settings reads. A bounded live run captured the prior
nonempty language, wrote and read back a temporary value, restored the original in `finally`, and
verified restoration with a fresh native `GetOrCreateAccount` call. Positive quota integers are
projected into the public limits model; raw optional values are preserved without inventing enum
semantics.

## Research method ledger

The service-free `research.proto` overlay copies the exact-package message and enum declarations
from the pinned `supported.proto`. The four async routes absent from the APK were independently
accepted by the Android bearer endpoint; `FinishDiscoverSourcesRun` and synchronous
`DiscoverSources` are also present in the committed mobile method manifest. The cumulative
`orchestration_service.proto` imports these messages and declares all six signatures. Five come
directly from the exact-package source. `CancelDiscoverSourcesJobRequest` also comes from that
source, and current-web constructor identity closes its response as `google.protobuf.Empty`; the
message overlay itself remains service-free because protobuf services cannot be reopened.

| Full method | Request / response | Replay policy |
| --- | --- | --- |
| `.../DiscoverSources` | `DiscoverSourcesRequest` / `DiscoverSourcesResponse` | never; quota-bearing discovery |
| `.../DiscoverSourcesManifold` | `DiscoverSourcesManifoldRequest` / `DiscoverSourcesManifoldResponse` | never; stateful start |
| `.../DiscoverSourcesAsync` | `DiscoverSourcesAsyncRequest` / `DiscoverSourcesAsyncResponse` | never; stateful start |
| `.../ListDiscoverSourcesJob` | `ListDiscoverSourcesJobRequest` / `ListDiscoverSourcesJobResponse` | replay-safe read; exact run ID selected locally |
| `.../CancelDiscoverSourcesJob` | `CancelDiscoverSourcesJobRequest` (`request_context #1`, job ID `#3`) / `google.protobuf.Empty` | never; ambiguity resolved only by exact-ID poll |
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
libraries and contains duplicate package-local persistence declarations. The checked-in read proto
sources instead preserve the exact wire packages from the descriptor/Dart library boundary and
copy only the fields below. Missing fields remain protobuf unknown fields; they are not filled from
plausible-looking flattened declarations. Chat follows the same rule: its exact-package message
overlay admits only fields retained by the named Dart protobuf libraries and checked against
captured wire tags. One cumulative `orchestration_service.proto` owns the exact orchestration
service, while `labs/language/tailwind/sharing/sharing.proto` owns the separately evidenced exact
sharing service. All implemented adapter paths are now generated and the machine-readable
[`grpc-service-signature-exceptions.json`](grpc-service-signature-exceptions.json) is empty. Seventeen
conventional request/response signatures derived from the current web registry are kept explicit in
[`grpc-service-signature-inferences.json`](grpc-service-signature-inferences.json).
An exact remote signature may still use a local runtime parser when live-only fields, heterogeneous
member bytes, scalar-presence semantics, or an exact field admitted through a cycle-free local
overlay exceeds the directly generated exact-package subset; those deliberate seams are separately recorded in
[`grpc-runtime-parser-overrides.json`](grpc-runtime-parser-overrides.json).

## Evidence input identities

The external exact-package snapshot was reviewed, reduced to the admitted fields, and then made
self-contained by this ledger, the checked-in proto sources, descriptor set, and synthetic
fixtures. Hashes prevent a later local checkout from silently changing what was admitted.

| Evidence input | SHA-256 | Role |
|---|---|---|
| exact-package orchestration `supported.proto` | `829c4ee871fd66421ee098fa266793ec68773e625ff005cc519b2c0f7c191ae9` | service/message FQNs, package, tags, cardinality, import origin |
| exact-package agency `supported.proto` | `991848507073973890527025f46ea3e0d35f86fad31ca52661394c54f371f643` | exact agency package/FQNs and `TailwindValue`, `TailwindStruct`, `FunctionCall`, and `FunctionResponse` field closure imported by `TailwindDoc` |
| exact-package `source_settings.proto` | `becd695c4281e23064c16fc1441c61117e5dc2a44c52cadf44af9e31c7cb8b18` | separate settings package, fields #2/#4, complete enums |
| exact-package sharing `supported.proto` | `f966dfebebe5eee213ad53607d2fddd44c8c33892f2a338d734491d9fb7b4309` | sharing service/message FQNs, tags, cardinality, and common-protos import |
| exact-package common `common.proto` | `0a2a7acbeebf3a97ad0fffa8b7496cb119c9f0fffb731011c47e9dba43313044` | exact `ChatSession` and `ProjectPublicSettings` message closure without duplicate declarations |
| [`schema.proto`](schema.proto) | `4d546eadc76aeca5b41e350ca11d11a943d7f2f89be9ff0de1f3d37eaf65eb07` | flattened Dart recovery used to identify gaps, never as a compile input; retains 15 zero-field messages and exact per-message package/library provenance |
| [`enums.txt`](enums.txt) | `fb138adfec1d701932f7efaee9f20f4fbb43b3df27acb00de91c169e659c5401` | exhaustive enum names and integers |
| blutter `pp.txt` | `2fc0bad6bee700cb628deb9ac1922eeea3d1255b51d8d2e1f63c5537d98965b0` | adjacent generated-client method paths, request/response generic bindings, and response constructors for the six formerly empty-response exceptions |
| blutter `ida_script/addNames.py` | `982fcbf1c5ef1d7d0aa9d5d0ae8af3c6e6a7c575af9bdba1fc3d7469aa8bc511` | exact protobuf Dart-library identity for `Empty`, `DeleteNotesResponse`, and `ShareProjectResponse`; summarized in [`blutter-grpc-signature-evidence.md`](blutter-grpc-signature-evidence.md) |
| [`grpc-capability-and-signature-evidence.md`](grpc-capability-and-signature-evidence.md) | `00091066e51b76c8e072100da8935de6b39cca30a8e7142ce11fb7a07b2ae15c` | consolidated signed-APK inventory, current authenticated Web-bundle signature inference, and mobile-backend route/semantic evidence; preserves the original report hashes and their distinct evidence boundaries |
| [`latest_apk_grpc_paths.txt`](../../tests/fixtures/android/latest_apk_grpc_paths.txt) | `b5df4996f271e71ccc14e0ae0f8eaa13e1e337b4bc726b54a487a0c4f6d31697` | complete 53-path `1.55.10` generated-client inventory, including the path-only unresolved `UpsertArtifactUserState` entry |
| [`latest_apk_grpc_signatures.csv`](../../tests/fixtures/android/latest_apk_grpc_signatures.csv) | `6381163929c18d51eb654bc677846061ea65e9d501b9beb9db3952b749b32b7c` | 52 exact `1.55.10` generated-client bindings with request/response FQNs and object-pool offsets |
| [`external_method_manifest.csv`](../../tests/fixtures/android/external_method_manifest.csv) | `411129064d2528b7ea108571ab382bd786055ed434209d6e733e13f130d9ebbd` | version-scoped `1.46.7` binary inventory plus independently live/web-proven signatures used by the implemented-adapter admission gate |
| [`chat-session-control-evidence.md`](chat-session-control-evidence.md) | `d348a05caa9fd61aff63caef1d506a08835d555d37edab1323382f152fa342d6` | live Web/Android status transitions, exact APK cancel binding, authorization boundary, and WEB-client-type cancellation qualification |
| [`public-api-audit.md`](public-api-audit.md) | `f2fbe716b0b95899737e3abda740009bc9a17fa8dd1a0b0702a18f07e06301b0` | dated 2026-08-29 public-adapter rejection inventory and disposable-copy validation; its three compatibility seams are superseded by the closure report |
| [`artifact-contracts-and-live-validation.md`](artifact-contracts-and-live-validation.md) | `58af0bbeebdfa6a6a7366577d90a5479bdf971a1ed76fe3d6d7d0b8420f8454d` | consolidated artifact generation, representation, data-table, retry/export, mind-map, and transfer evidence; preserves all four source-report hashes and cleanup qualifications |
| [`file-transfer-evidence.md`](file-transfer-evidence.md) | `3752ef8cf75e3fcafaca3522a28a323c01d931c4d9f4ca39eb2d5ddb0679d2b9` | official-app/headless PDF upload request, qualified CSV/DOCX compatibility boundary, and live artifact representation/direct infographic/slide transfer |
| [`resource-lifecycle-and-public-qualification.md`](resource-lifecycle-and-public-qualification.md) | `bf66c01d168e2cb8f191a97670d767c796681610b9a649c891e8439a27117526` | consolidated notebook copy/metadata, note/mind-map, label/collection, membership, cleanup, and public-qualification evidence; preserves all four source-report hashes |
| [`endpoints.md`](endpoints.md) | `f7842e7450380d233d84512dfc5b046a99730db346f4dd87315ebaf7ef84ab5c` | live request/response envelopes, route results, version-scoped APK inventories, captured note/sharing bytes, and the account-bootstrap replay boundary |

The recovery method and the warning about duplicate packages are committed in
[`README.md`](README.md#caveats-that-will-bite-you). Live request/response shapes are documented in
[`endpoints.md`](endpoints.md#getproject).

## Cumulative generated service ledger

| Full method | Request FQN | Response FQN | Cardinality | Request fields populated |
|---|---|---|---|---|
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GetProject` | `.google.internal.labs.tailwind.orchestration.v1.GetProjectRequest` | `.google.internal.labs.tailwind.orchestration.v1.GetProjectResponse` | unary/unary | `project_id #1`, `include_audio_overview_ids #2`; no `RequestContext` |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/ListRecentlyViewedProjects` | `.google.internal.labs.tailwind.orchestration.v1.ListRecentlyViewedProjectsRequest` | `.google.internal.labs.tailwind.orchestration.v1.ListRecentlyViewedProjectsResponse` | unary/unary | `include_own_projects #2`, `include_audio_overview_ids #3`; no `RequestContext` |

The two read signatures above and fifty-five later exact or web-derived signatures live in the sole
`google/internal/labs/tailwind/orchestration/v1/orchestration_service.proto` service declaration.
The individual message overlays remain service-free so protobuf never reopens one service across
files. Its generated stub exposes 57 implemented methods. The exact sharing service adds
`GetProjectDetails` and `ShareProject`, producing 59 generated paths across the two services. Seventeen
signatures retain explicit web-derived type-name provenance; the signature-exception manifest is
empty. The inference and runtime-parser manifests name each adapter seam and evidence link;
bidirectional descriptor/adapter/external-manifest equality is pinned by
`tests/unit/android/test_grpc_service_manifest.py`.

The notebook contract's pinned exact-package source and external method manifest admit `CreateProject`,
`DeleteProjects`, `MutateProject`, `RemoveRecentlyViewedProject`, and `GenerateNotebookGuide` to the
generated service. Blutter's
generated-client binding proves that `DeleteProjects` returns `google.protobuf.Empty`; `CopyProject`
uses a generated conventional request name inferred from the current web registry, with the response
proven as the same exact-package `Project` constructor used by create/mutate. Runtime uses
local parsers only for the live emoji field and captured guide-topic field described below.
The durable reduced compile input is
[`notebooks.proto`](../../src/notebooklm/_android/proto_src/google/internal/labs/tailwind/orchestration/v1/notebooks.proto)
(SHA-256 `2b4188e023682e8500f2ea646211de4f371ff787b517435061261e268baf0123`);
the cumulative service reuses the established exact `Project` from `read.proto` rather than
duplicating it in the notebook source.

## Notebook method ledger

`Wire*` means a message in the deliberately non-Google package
`notebooklm.internal.android.wire.v1`. It claims wire equivalence only. The durable exact-package
`notebooks.proto` owns admitted method messages; local wire messages remain only where the adapter
needs a field absent from that archived semantic subset. Bare `Project` responses reuse the read
exact-package message, and empty deletion uses `google.protobuf.Empty` directly.

| Full method | Request parser | Response parser | Replay | Evidence |
|---|---|---|---|---|
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/CreateProject` | exact-package `CreateProjectRequest` | exact-package `Project` (bare) | never in transport; base create probes before retry | [`endpoints.md`](endpoints.md#createproject--create-a-notebook) |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/DeleteProjects` | exact-package `DeleteProjectsRequest` | exact `google.protobuf.Empty` | never | [`blutter-grpc-signature-evidence.md`](blutter-grpc-signature-evidence.md#exact-recovered-bindings) |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/MutateProject` | local `WireMutateProjectRequest` runtime override for live-only emoji #3; remote FQN is exact | exact-package `Project` (bare) | never | [`endpoints.md`](endpoints.md#mutateproject--rename--edit-notebook-fields) |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/CopyProject` | generated web-derived `CopyProjectRequest` | exact-package `Project` (bare) | never; transport ambiguity is surfaced | [web signature inference](grpc-capability-and-signature-evidence.md#signature-matrix) |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GenerateNotebookGuide` | exact-package `GenerateNotebookGuideRequest` | local `WireGenerateNotebookGuideResponse` runtime override for captured topic field #2; remote FQN is exact | never; stateful | [`endpoints.md`](endpoints.md#generatenotebookguide) |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/RemoveRecentlyViewedProject` | exact-package `RemoveRecentlyViewedProjectRequest` | exact `google.protobuf.Empty` | never; succeeds for genuinely shared projects; an owned project returns `INTERNAL`, which the public adapter folds into the same already-absent no-op exposed by Web | [`web-compat-seam-closure.md`](web-compat-seam-closure.md#notebooksremove_from_recent--wrong-resource) |

## Artifact service ledger

The archived artifact declarations in `artifacts.proto` were independently checked against the
exact-package `supported.proto` whose SHA-256 is pinned above. The separately marked
`GenerateReportSuggestions` closure is derived from the current web bundle instead. References to
the flattened `schema.proto` in source comments are corroborating Dart-symbol evidence, never the
sole authority for a Google FQN.
The artifact message overlay intentionally declares no second protobuf `service`: protobuf cannot reopen
the same service across files. The cumulative service imports all nine artifact-specific signatures
plus the APK-exact `ActOnSources` signature used for note-backed mind maps. Blutter's
generated-client binding proves `DeleteArtifact` returns
`google.protobuf.Empty`; `GenerateReportSuggestions`, `GenerateArtifact`, and `ExportToDrive` use
generated conventional type names recorded in the inference manifest. `AndroidSession` continues
to dispatch paths generically with the ledgered message classes.

| Full method | Exact request FQN | Exact response FQN | Artifact disposition |
|---|---|---|---|
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/ListArtifacts` | `.google.internal.labs.tailwind.orchestration.v1.ListArtifactsRequest` | `.google.internal.labs.tailwind.orchestration.v1.ListArtifactsResponse` | admitted safe aggregate Studio read |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GetArtifact` | `.google.internal.labs.tailwind.orchestration.v1.GetArtifactRequest` | `.google.internal.labs.tailwind.orchestration.v1.GetArtifactResponse` | admitted safe single-artifact polling read; common SDK `get` remains concrete over aggregate `list` so note-backed mind maps remain visible |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/CreateArtifact` | `.google.internal.labs.tailwind.orchestration.v1.CreateArtifactRequest` | `.google.internal.labs.tailwind.orchestration.v1.CreateArtifactResponse` | evidence-qualified quiz and Audio Overview mutations, never replayed |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/DeriveArtifact` | `.google.internal.labs.tailwind.orchestration.v1.DeriveArtifactRequest` | `.google.internal.labs.tailwind.orchestration.v1.DeriveArtifactResponse` | APK-exact slide derivation, live-successful on a disposable copy, never replayed |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/UpdateArtifact` | `.google.internal.labs.tailwind.orchestration.v1.UpdateArtifactRequest` | `.google.internal.labs.tailwind.orchestration.v1.Artifact` | title-only mutation with etag, never replayed, then list read-back |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/DeleteArtifact` | `.google.internal.labs.tailwind.orchestration.v1.DeleteArtifactRequest` | `.google.protobuf.Empty` | admitted exact signature; never replayed; sanitized `NOT_FOUND` is idempotent success |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GenerateReportSuggestions` | generated web-derived `.google.internal.labs.tailwind.orchestration.v1.GenerateReportSuggestionsRequest` | generated web-derived `.google.internal.labs.tailwind.orchestration.v1.GenerateReportSuggestionsResponse` | safe live-added read; conventional names remain explicit in inference manifest |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GenerateArtifact` | generated web-derived `.google.internal.labs.tailwind.orchestration.v1.GenerateArtifactRequest` | generated web-derived `.google.internal.labs.tailwind.orchestration.v1.GenerateArtifactResponse` | failed-artifact retry; live route and precondition behavior pinned; never replayed |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/ExportToDrive` | generated web-derived `.google.internal.labs.tailwind.orchestration.v1.ExportToDriveRequest` | generated web-derived `.google.internal.labs.tailwind.orchestration.v1.ExportToDriveResponse` | report-to-Docs live-successful with read-back and exact cleanup; never replayed |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/ActOnSources` | APK-exact `.google.internal.labs.tailwind.orchestration.v1.ActOnSourcesRequest` | APK-exact `.google.internal.labs.tailwind.orchestration.v1.ActOnSourcesResponse` | note-backed mind-map JSON generation; current-bundle action branch `#6` and Android-bearer success pinned; never replayed, then native `CreateNote` in the same epoch |

## Artifact exact field ledger

This table is exhaustive for `artifacts.proto`; fields present in the archived message but not
needed by the adapter are deliberately left unknown. “Exact” means the pinned exact-package archive, not a
field inferred from its plausible Dart name.

| Exact-package message | Admitted fields (`name #tag`, cardinality and type) | Adapter use |
|---|---|---|
| `MediaStreamingUrl` | `url #1` string; `type #2` `MediaStreamingType` | audio/video representation projection |
| `QuizGenerationOptions` | `question_quantity #1` `QuestionQuantity`; `quiz_difficulty #2` `QuizDifficulty` | quiz create options |
| `AppArtifactGenerationOptions` | `app_type #1` `AppType`; `free_text_steering_prompt #3`; `language_code #4`; flashcard options `#7`; quiz options `#8` | quiz, flashcard, and interactive-mind-map create options |
| `TemplatizedApp` / `AppArtifact` | templated data `#1`; app HTML `#1`, options `#2`, templated app `#3`, live mind-map JSON `#4` | quiz/flashcard local saves and interactive mind-map tree/read/download |
| `EpisodeLength` | exhaustive values: unspecified `0`, short `1`, medium `2`, long `3` | Audio Overview creation length |
| `AudioOverviewGenerationOptions` | `episode_focus #1` string; `episode_length #2` `EpisodeLength`; repeated `source_ids #4` imported exact `SourceId`; `language_code #5` string; local wire overlay supplies live-proven format code `#7` | Audio Overview create and existing prompt/source projections across all four formats |
| `AudioOverviewArtifact` | `generation_options #2` message; `is_interactive #5` bool; repeated `media_urls #6`; `duration #7` `google.protobuf.Duration` | create/list/poll projection; `is_interactive` is not projected because public `Artifact` has no corresponding field |
| `ExplainerVideoGenerationOptions` | `video_focus #3` string | listing prompt |
| `ExplainerVideoArtifact` | `generation_options #3` message; repeated `media_urls #5`; `duration #6` `google.protobuf.Duration` | listing projection only |
| `TailoredReportArtifactGenerationOptions` | `type #1`; `description #2`; repeated source IDs `#4`; language `#5`; document directive `#6` | report kind/create/prompt |
| `TailoredReportArtifact` | options `#2`; exact `TailwindDoc report_doc #3` | listing and local Markdown rendering |
| `ServedImage` | `url #1` string | infographic/slide representation |
| `InfographicGenerationOptions` | `user_steering_prompt #1` string | listing prompt |
| `Infographic` | `title #1` string; `image #2` `ServedImage` | PNG selection |
| `InfographicArtifact` | `generation_options #1` message; repeated `infographics #3` | listing/download projection |
| `SlidesGenerationOptions` | steering prompt `#1`; language `#2`; deck type `#3`; length `#4` | listing and create options |
| `Slide` | `image #1` `ServedImage` | listing projection |
| `SlidesArtifact` | options `#1`; repeated slides `#3`; PDF URL `#4`; PPTX URL `#5` | listing and strict PDF/PPTX transfer |
| `SlideEditInstruction` / `SlidesDerivationOptions` | slide index `#1`, instruction `#2`; repeated instructions `#1` | exact slide revision payload |
| `FileArtifact` | file name `#1`, MIME type `#2`, preview URL `#3`, download URL `#4` | full file representation projection |
| `AudioOverviewState` / `VideoOverviewState` | audio playback position `#1` Duration; video state is an exact zero-field marker | audio playback projection; populated video state remains an unknown public state |
| `AppArtifactState` / `ScheduledNotificationConfig` | app state `#1` Struct; notification config is an exact zero-field marker | flashcard progress projection and lossless unknown-state fallback |
| `ArtifactState` | audio `#1`; video `#2`; app `#3`; repeated scheduled notification configs `#4` | exact user-state envelope |
| `ArtifactSource` | `source_id #1` imported exact `SourceId` | source IDs and quiz/audio requests |
| `Artifact` | `artifact_id #1` string; `title #2` string; `type #3` `ArtifactType`; repeated `sources #4`; `status #5` `ArtifactStatus`; `audio_overview #7`; `tailored_report #8`; `explainer_video #9`; `app #10`; `last_modified_timestamp #11` Timestamp; `infographic #15`; `slides #17`; `artifact_user_state #18`; `etag #22` string; `file #25` | the complete artifact public projection and mutation subset, including typed audio/flashcard state |
| `CreateArtifactRequest` / `Response` | request `project_id #2`, `artifact #3`; response `artifact #1` | quiz and Audio Overview mutations |
| `GenerateArtifactRequest` / `Response` | request context `#1`, artifact ID `#2`; response artifact `#1` | failed-artifact retry through a web-derived conventional FQN |
| `ExportToDriveRequest` / `Response` | request context `#1`, oneof artifact ID `#2` or literal content `#3`, title `#4`, destination `#5`; response URL `#1` | report/table export through a web-derived conventional FQN |
| `DeriveArtifactRequest` / `Response` | request context `#1`, original artifact ID `#2`, slide options `#3`; response artifact `#1` | live slide revision |
| `GetArtifactRequest` / `Response` | request `artifact_id #1`; response `artifact #1` | exact single-artifact polling primitive; not substituted for aggregate public `get` |
| `ListArtifactsRequest` / `Response` | request `project_id #2`; response repeated `artifacts #1` | Studio aggregate listing |
| `UpdateArtifactRequest` | `artifact #1`; `update_mask #2` FieldMask; `etag #3` string | title rename |
| `DeleteArtifactRequest` | `artifact_id #2` string | idempotent delete |

The nine exact top-level enums are also pinned exhaustively by generated descriptor tests:
`ArtifactType` 0–10, `ArtifactStatus` 0–6, `AppType` 0–5, `EpisodeLength` 0–3,
`MediaStreamingType` 0–4, `VideoOverviewTemplateFormat` 0–5, `VideoOverviewStyle` 0–9,
`DeckType` 0–2, and `SlideDeckLength` 0–4. The four nested quiz/flashcard enums are 0–3.
Unknown future integers remain unknown rather than being coerced to a known family.

### Artifact quiz request

The successful quiz branch sends `CreateArtifactRequest.project_id #2` and `artifact #3`, with
`Artifact.type = ARTIFACT_TYPE_APP`, repeated `Artifact.sources #4`, and
`AppArtifact.generation_options.app_type = APP_TYPE_QUIZ`. Quantity/difficulty use the exact nested
enums at quiz option fields #1/#2; free text uses app option field #3. No other family builder is
admitted in the artifact quiz slice.

### Artifact Audio Overview request

The captured Audio Overview branch uses the same exact `CreateArtifact` method with
`Artifact.type = ARTIFACT_TYPE_AUDIO_OVERVIEW`. It requires at least one source ID and a non-empty
language code. Every ordered source ID is duplicated in top-level `Artifact.sources #4` and nested
`AudioOverviewGenerationOptions.source_ids #4`; focus is field `#1`, `EpisodeLength` is field `#2`,
and language is field `#5`. Public `AudioLength.SHORT`, `DEFAULT`, and `LONG` map exactly to enum
values `1`, `2`, and `3`; omitted length sends medium/default `2`. The captured exact closure omits
the audio-format field, so a narrow local wire overlay supplies field `#7`: omitted format and
`DEEP_DIVE` send `1`, while `BRIEF`, `CRITIQUE`, and `DEBATE` send `2`, `3`, and `4`. Authenticated
Android calls accepted and echoed codes 2, 3, and 4. Creation is non-replay-safe and source
resolution plus mutation share one lifecycle operation/epoch lease.

The exact `GetArtifact` request carries only `artifact_id #1` in the admitted closure and is used
for one safe polling read per tick. The public aggregate `get`/`get_or_none` methods deliberately
remain list-based so their established note-backed mind-map semantics are unchanged.

### Artifact web-derived report suggestion closure

`GenerateReportSuggestions` is live-successful but absent from the APK method/symbol archive. The
current web registry proves distinct request/response constructors and call sites pin request
context/project/repeated source IDs at `#1/#2/#3`. The response carries repeated suggestion rows at
`#1`; each row carries title/description/repeated source IDs/prompt/audience level at
`#1/#2/#4/#5/#6`. An accessed nested field at `#3` remains semantically unrecovered and therefore
undeclared. artifact now compiles that admitted partial closure under conventional Google-package type
names and records the naming inference explicitly. The adapter populates Android request context
and project; its public method currently leaves the optional source filter empty.

## Artifact representation-transfer evidence boundary

Only response-provided URLs are projected: audio field #6, video field #5, infographic image #2,
slide image #1, slide PDF/PPTX #4/#5, and file preview/download #3/#4. No URL is synthesized from an
artifact kind. Infographic PNG and progressive MP4/WAV terminal bytes are live-verified. Bearer
authentication is limited to exact `lh3.googleusercontent.com` and is stripped before capability
and `.googlevideo.com` hosts. Every representation has a strict host, MIME, and magic-signature
policy plus same-directory atomic publication. PDF/PPTX URL selection is implemented, but current
slide transfer uses the APK-evidenced SSO HTTP GET with `alr=yes`, not a multipart form-token
exchange. Authentication is retained only for configured Google storage hosts and stripped on
redirect to untrusted origins. A live bearer GET to `contribution.usercontent.google.com` returned
HTTP 200 octet-stream; public Android downloads then produced a 15,017,608-byte `%PDF-` document
and a 17,392,113-byte valid OOXML ZIP/PPTX. The anonymous control returned a 302 to sign-in HTML,
which remains rejected rather than written.

### Chat method ledger

The chat message overlay remains service-free because protobuf services cannot be extended across
source files. The cumulative service imports the exact `ListChatSessions`, `ListChatTurns`,
`DeleteChatTurns`, and `GenerateFreeFormStreamed` signatures. The recovered generated-client
binding proves that `DeleteChatTurns` returns `google.protobuf.Empty`.

| Full method | Request overlay | Response deserializer | Cardinality | Retry/telemetry contract |
|---|---|---|---|---|
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/ListChatSessions` | `.google.internal.labs.tailwind.orchestration.v1.ListChatSessionsRequest` | `.google.internal.labs.tailwind.orchestration.v1.ListChatSessionsResponse` | unary/unary | replay-safe |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/ListChatTurns` | `.google.internal.labs.tailwind.orchestration.v1.ListChatTurnsRequest` | `.google.internal.labs.tailwind.orchestration.v1.ListChatTurnsResponse` | unary/unary | replay-safe |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/DeleteChatTurns` | `.google.internal.labs.tailwind.orchestration.v1.DeleteChatTurnsRequest` | exact `.google.protobuf.Empty` | unary/unary | non-replay-safe |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GenerateFreeFormStreamed` | `.google.internal.labs.tailwind.orchestration.v1.GenerateFreeFormStreamedRequest` | `.google.internal.labs.tailwind.orchestration.v1.GenerateFreeFormStreamedResponse` | unary/server-streaming | no retry; one aggregate deadline; `telemetry_method=None` |

## Notes and sharing service ledger

The full method paths below come from the exact method inventory and were routed live. Note CRUD
also has valid-resource semantic proof on a disposable copied notebook: create and mutate read back
the exact title/content, and delete was eventually visible (the first read could retain the row;
the next excluded it). Request context fields were optional in that ordinary-note replay and remain
omitted for plain CRUD; citation-rich saved responses populate current-bundle CreateNote context
`#7`. The cumulative orchestration service admits all four Notes signatures. The generated
client binds `DeleteNotes` to the exact zero-field
`google.internal.labs.tailwind.orchestration.v1.DeleteNotesResponse`.
The pinned sharing source and recovered client bindings fully prove both sharing signatures, which
are admitted to the separately generated exact sharing service. Current authenticated Web
constructors close the collaborator rows and user-mutation branches omitted by the older recovered
subset. The `GetProjectDetails` runtime response parser remains local only to preserve proto3
scalar presence for fields #3/#4 while decoding the same exact collaborator tags.
The durable reduced sharing source is
[`sharing.proto`](../../src/notebooklm/_android/proto_src/labs/language/tailwind/sharing/sharing.proto)
(SHA-256 `57c42c7c4e30eeeccc049f3de8cb72aaa70f41a4cb3cfc9e69d602fb6e4927b9`);
it imports the exact chat
[`common.proto`](../../src/notebooklm/_android/proto_src/labs/language/tailwind/common/protos/common.proto)
so `ProjectPublicSettings` has one declaration.

| Full method | Request/response evidence | Replay policy | Public projection |
|---|---|---|---|
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GetNotes` | exact-package `GetNotesRequest` / `GetNotesResponse`; two same-ID cross-backend live runs | safe read | ordinary notes exclude prompt-typed and JSON-shaped maps; private minimal map rows expose exact `[id, content]` only and do not claim full Web raw-row parity |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/CreateNote` | exact-package `CreateNoteRequest` / `CreateNoteResponse` | never replay | create, then exact read-back |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/MutateNote` | exact-package `MutateNoteRequest` / `MutateNoteResponse` | never replay | existence preflight, edit, exact read-back |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/DeleteNotes` | exact `DeleteNotesRequest` / exact zero-field `DeleteNotesResponse`; live map deletion preserved an ordinary sibling and a second delete succeeded | never replay | kind-safe idempotent preflight plus bounded eventual-absence reads for notes and note-backed maps |
| `/labs.language.tailwind.sharing.LabsTailwindSharingService/GetProjectDetails` | exact request/response FQNs and generated service; local response-parser override for scalar presence | safe read | collaborator rows #1, public settings #2, cap #3, and policy #4 |
| `/labs.language.tailwind.sharing.LabsTailwindSharingService/ShareProject` | exact `ShareProjectRequest` / exact zero-field `ShareProjectResponse` | never replay | public readability or collaborator grants/removals, then exact `GetProjectDetails` read-back |

`GetNotes` status 5 maps to `NotebookNotFoundError`; a missing listed note maps to
`NoteNotFoundError`. Create/share status 5 maps to the notebook miss. Mutate status 5 maps to the
note miss after its existence preflight. A delete status 5 after a successful preflight is the
idempotent concurrent-absence outcome.

Each of the eight manifest-shaped callables on the selected Android Notes adapter owns exactly one
supervisor operation scope.
Create/update read-back and both delete preflight/poll workflows pass that scope's `expected_epoch`
to every unary call instead of composing through another public method. The private typed
note-backed map read used by mind-map is scoped independently as well. Graceful drain therefore admits
the already-started workflow through its final read, cancellation settles the complete operation,
and a forced close/reopen rejects any old-epoch read-back or poll before it can touch the new
transport generation.

## Import closure

| Import | Exact package | Why reachable |
|---|---|---|
| `google/internal/labs/tailwind/v1/source_settings.proto` | `google.internal.labs.tailwind.v1` | `Source.settings #4`; Dart library boundary is `source_settings.pb.dart` |
| `google/internal/labs/tailwind/orchestration/v1/account.proto` | `google.internal.labs.tailwind.orchestration.v1` | exact `TierLimits` reused by `Project.project_tier_limits #11` |
| `google/protobuf/timestamp.proto` | `google.protobuf` | `ProjectMetadata.create_time #9` |
| `labs/language/tailwind/common/protos/common.proto` | `labs.language.tailwind.common.protos` | `Project.chat_sessions #12`; exact APK field and live CreateProject response |

No agency, artifact, chat, mutation, context, Empty, Struct, Duration, or FieldMask proto is
reachable from the read projection.

Notes add the exact `google.protobuf.Timestamp`-typed `last_edit_timestamp` field but deliberately
does not project it as public `Note.created_at`: a last edit is not evidence of creation time. The
sharing and zero-byte response types need no external imports because their overlays are local.

## Field ledger

`singular` means ordinary proto3 scalar/message presence; `repeated` is the only repeated
cardinality admitted. Every row is asserted exhaustively against the generated descriptors.

| Package.Message | Field | Tag | Cardinality | Type/evidence |
|---|---|---:|---|---|
| `orchestration.v1.SourceId` | `id` | 1 | singular | string; exact-package closure + Dart symbol |
| `orchestration.v1.WebpageMetadata` | `url` | 1 | singular | string; exact-package closure + Dart symbol |
| `orchestration.v1.GoogleDocsSourceMetadata` | `document_id` | 1 | singular | string; exact companion `supported.proto`; primary native Docs identity |
| `orchestration.v1.GoogleDriveSourceMetadata` | `document_id` | 1 | singular | string; exact-package closure + Dart symbol |
| `orchestration.v1.GoogleDriveSourceMetadata` | `mime_type` | 3 | singular | string; exact-package closure + Dart symbol |
| `orchestration.v1.ExpertIntelligenceSourceMetadata` | `content_id`, `title`, `authors`, `thumbnail_image_url`, `description`, `field_type` | 1, 3, 4, 5, 6, 7 | singular except repeated authors | exact APK schema; field type remains semantically opaque |
| `orchestration.v1.SourceMetadata` | `google_docs_metadata` | 1 | singular | `GoogleDocsSourceMetadata`; exact companion closure |
| `orchestration.v1.SourceMetadata` | `source_added_timestamp` | 3 | singular | `.google.protobuf.Timestamp`; exact APK schema + live `GetProject` response |
| `orchestration.v1.SourceMetadata` | `original_source_content_type` | 5 | singular | `OriginalSourceContentType`; complete values in [`enums.txt`](enums.txt) |
| `orchestration.v1.SourceMetadata` | `webpage_metadata` | 8 | singular | `WebpageMetadata`; exact-package closure + Dart symbol |
| `orchestration.v1.SourceMetadata` | `google_drive_source_metadata` | 10 | singular | `GoogleDriveSourceMetadata`; exact-package closure + Dart symbol |
| `orchestration.v1.SourceMetadata` | `expert_intelligence_source_metadata` | 19 | singular | `ExpertIntelligenceSourceMetadata`; exact APK schema |
| `orchestration.v1.Source` | `source_id` | 1 | singular | `SourceId`; exact-package closure + Dart symbol |
| `orchestration.v1.Source` | `title` | 2 | singular | string; exact-package closure + Dart symbol |
| `orchestration.v1.Source` | `metadata` | 3 | singular | `SourceMetadata`; exact-package closure + Dart symbol |
| `orchestration.v1.Source` | `settings` | 4 | singular | `.google.internal.labs.tailwind.v1.SourceSettings`; exact import origin |
| `tailwind.v1.SourceSettings` | `status` | 2 | singular | `SourceStatus`; `source_settings.pb.dart` + complete enum dump |
| `tailwind.v1.SourceSettings` | `user_drive_source_status` | 4 | singular | `UserDriveSourceStatus`; `source_settings.pb.dart` + complete enum dump |
| `orchestration.v1.ProjectMetadata` | `user_role` | 1 | singular | `ProjectRole`; complete enum dump |
| `orchestration.v1.ProjectMetadata` | `create_time` | 9 | singular | `.google.protobuf.Timestamp`; exact-package closure + Dart symbol |
| `orchestration.v1.ProjectMetadata` | `is_public` | 13 | singular | bool; exact APK schema |
| `orchestration.v1.ProjectMetadata` | `audio_overview_artifact_ids` | 17 | repeated | string; exact APK schema |
| `orchestration.v1.Project` | `title` | 1 | singular | string; exact-package closure + Dart symbol |
| `orchestration.v1.Project` | `sources` | 2 | repeated | `Source`; exact-package closure + live response cardinality |
| `orchestration.v1.Project` | `id` | 3 | singular | string; exact-package closure + Dart symbol |
| `orchestration.v1.Project` | `emoji` | 4 | singular | string; exact-package closure + live response |
| `orchestration.v1.Project` | `metadata` | 6 | singular | `ProjectMetadata`; exact-package closure + Dart symbol |
| `orchestration.v1.Project` | `premium_feature_info` | 10 | singular | `PremiumFeatureInfo`; exact APK schema plus live list responses |
| `orchestration.v1.Project` | `project_tier_limits` | 11 | singular | exact `TierLimits`; exact APK schema |
| `orchestration.v1.Project` | `chat_sessions` | 12 | repeated | exact `common.protos.ChatSession`; APK field plus live CreateProject response |
| `orchestration.v1.GetProjectRequest` | `project_id` | 1 | singular | string; exact service closure + successful capture |
| `orchestration.v1.GetProjectRequest` | `include_audio_overview_ids` | 2 | singular | bool; exact service closure + successful capture |
| `orchestration.v1.GetProjectResponse` | `project` | 1 | singular | `Project`; exact wrapper declaration |
| `orchestration.v1.ListRecentlyViewedProjectsRequest` | `include_own_projects` | 2 | singular | bool; exact service closure + successful capture |
| `orchestration.v1.ListRecentlyViewedProjectsRequest` | `include_audio_overview_ids` | 3 | singular | bool; exact service closure + successful capture |
| `orchestration.v1.ListRecentlyViewedProjectsResponse` | `projects` | 1 | repeated | `Project`; exact wrapper declaration |

The public Android source decoder prefers nonempty `google_docs_metadata.document_id #1` and falls
back to `google_drive_source_metadata.document_id #10/#1`. This mirrors the two exact metadata
locations without discarding the Drive descriptor's MIME field, and preserves the public
`drive_document_id` used to correlate native Docs/Slides/Sheets references with their Drive file.

The exact companion `SourceMetadata` also contains `expert_intelligence_source_metadata #19`, whose
content ID, authors, thumbnail, description, and opaque numeric field have no corresponding typed
`Source` attributes. They remain absent from that typed model rather than being mislabeled as
revision/download metadata, while `notebooks.get_raw()` preserves the admitted backend-shaped
fields. Source creation time is projected from `source_added_timestamp #3` to public `Source.created_at`.
Conversely, public download/viewer URLs, word count, revision ID/timestamp,
and last-modified time have no exact field in this `Source` closure and therefore remain
`None`. The Google Docs identity and `source_added_timestamp` are the concrete exact fields that map
to the current public `Source` contract.

### Notebook exact and web-derived field ledger

Every row below is asserted against the generated descriptor and deterministic serialized bytes.
The web-derived copy request populates the observed Android request context; older exact requests
retain their established builders.

| Message | Field | Tag | Cardinality | Type/evidence |
|---|---|---:|---|---|
| exact `CreateProjectRequest` | `name` | 1 | singular | string; pinned source and captured create request |
| exact `DeleteProjectsRequest` | `project_ids` | 1 | repeated | string; pinned source and captured single-ID delete request |
| exact `ProjectMutation.ChangePropertyMutation` | `new_title` | 2 | singular | string; pinned source and captured title mutation |
| exact `ProjectMutation` | `change_property` | 4 | singular | exact nested message; pinned source |
| exact `MutateProjectRequest` | `project_id`, `mutations` | 1, 2 | singular, repeated | string plus exact mutation; pinned source |
| `WireProjectChangeProperty` | `new_title` | 2 | singular | string; captured title-only mutation |
| `WireProjectChangeProperty` | `new_emoji` | 3 | optional | string; repository-local name, live set/clear/combined mutation plus bare-response and `GetProject` read-back |
| `WireProjectMutation` | `change_property` | 4 | singular | local nested message; captured mutation variant |
| `WireMutateProjectRequest` | `project_id` | 1 | singular | string; captured mutation request |
| `WireMutateProjectRequest` | `mutations` | 2 | repeated | local nested message; captured cardinality |
| web-derived `CopyProjectRequest` | `request_context` | 1 | singular | exact common `RequestContext`; current-web constructor field |
| web-derived `CopyProjectRequest` | `source_project_id` | 2 | singular | string; direct successful replay |
| web-derived `CopyProjectRequest` | `title` | 3 | singular | string; direct successful replay |
| exact `GenerateNotebookGuideRequest` | `project_id` | 1 | singular | string; pinned source and captured stateful request |
| exact `RemoveRecentlyViewedProjectRequest` | `project_id`, `request_context` | 1, 2 | singular | official APK exact binding; the owned-project probe returned `INTERNAL`, while later two-account evidence established native shared-project removal and the owned-project no-op rule |
| exact `NotebookSummary` | `text_summary` | 1 | singular | string; pinned source and captured guide response |
| exact `NextStep` / `NextStepSuggestions` | `suggestion`, `suggestion_type` / `next_steps` | 1, 2 / 1 | singular / repeated | string plus exact `MagicArtifactType`; pinned semantic guide closure |
| exact `NotebookGuide` / `GenerateNotebookGuideResponse` | `summary`, `next_step_suggestions` / `notebook_guide` | 1, 6 / 1 | singular | pinned semantic response FQN closure |
| `WireSuggestedTopic` | `question` | 1 | singular | string; captured guide topic row |
| `WireSuggestedTopic` | `prompt` | 2 | singular | string; captured guide topic row |
| `WireSuggestedTopics` | `topics` | 1 | repeated | local topic row; captured response cardinality |
| `WireNotebookGuide` | `summary` | 1 | singular | imported exact `NotebookSummary`; captured response |
| `WireNotebookGuide` | `suggested_topics` | 2 | singular | local topic envelope; captured response |
| `WireGenerateNotebookGuideResponse` | `notebook_guide` | 1 | singular | local guide message; captured response wrapper |

### Chat field ledger

The compile inputs are
[`chat.proto`](../../src/notebooklm/_android/proto_src/google/internal/labs/tailwind/orchestration/v1/chat.proto)
(SHA-256 `4f343dd12cbce225fc727ac9e5bc88a898db831d322de0ef2d23382af5ee29a1`) and
[`common.proto`](../../src/notebooklm/_android/proto_src/labs/language/tailwind/common/protos/common.proto)
(SHA-256 `7d064bf11e3f01465e485004e6dbba078ae9b92f02be53ac2a8a4ac6a420af75`).
Chat request context imports
[`metadata.proto`](../../src/notebooklm/_android/proto_src/labs/language/tailwind/common/protos/metadata.proto)
(SHA-256 `be554c0439e06da7c5cd962356c5af7c3ba4f7855ef969104ede5c1fb56a4366`).
The exact agency value closure is imported from
[`agency/supported.proto`](../../src/notebooklm/_android/proto_src/google/internal/labs/tailwind/orchestration/v1/agency/supported.proto)
(SHA-256 `991848507073973890527025f46ea3e0d35f86fad31ca52661394c54f371f643`),
which is byte-identical to the pinned archived exact-package input above.
The shared `InputSource` declaration is imported from
[`sources.proto`](../../src/notebooklm/_android/proto_src/google/internal/labs/tailwind/orchestration/v1/sources.proto)
rather than redeclared in the chat overlay.
The exact `NextStepSuggestions` declaration is imported from
[`notebooks.proto`](../../src/notebooklm/_android/proto_src/google/internal/labs/tailwind/orchestration/v1/notebooks.proto);
the checked-in APK schema proves its streamed placement at response field 6.
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
| `orchestration.v1.ActOnSourcesResponse` | `response` (1; `AnswerResponse`), `next_step_suggestions` (6; `NextStepSuggestions`) |
| `orchestration.v1.ActOnSourcesRequest` | `sources` (1; repeated `InputSource`), `options` (2; `ActOnSourcesOptions`), `free_form_action` (3; `FreeFormAction`), `mind_map_action` (6; current-bundle `ActOnSourcesMindMapAction`), `source_options` (7; `InputSourceOptions`), `request_context` (8; `RequestContext`), `chat_session_id` (10; string), `origin` (11; `QueryOrigin`) |
| `orchestration.v1.ActOnSourcesMindMapAction` | `action` (1; string), `context` (2; repeated key/value rows), `language` (3; string) |
| `orchestration.v1.DeleteChatTurnsRequest` | `chat_session_id` (2; string), `delete_all_history` (4; bool) |
| `orchestration.v1.GetChatSessionStatusRequest/Response` | `request_context` (1; `RequestContext`), `chat_session_id` (2; string); `generation_token` (1; string), `status` (2; int32) |
| `orchestration.v1.CancelGenerationRequest/Response` | `request_context` (1; `RequestContext`), `chat_session_id` (2; string), `agency_session_id` (3; string); named empty response |
| `orchestration.v1.GenerateFreeFormStreamedRequest` | `sources` (1; repeated `InputSource`), `user_query` (2; string), `conversation_history` (3; repeated `ConversationEvent`), `request_context` (4; `RequestContext`), `chat_session_id` (5; string), `user_message_id` (6; string), `project_id` (8; string), `origin` (9; `QueryOrigin`) |
| `orchestration.v1.GenerateFreeFormStreamedResponse` | `answer` (1; `AnswerResponse`), `is_final_response` (5; bool), `next_step_suggestions` (6; `NextStepSuggestions`) |
| `orchestration.v1.AnswerResponse` | `response` (1; string), `conversation_turn_key` (3; `ConversationTurnKey`), `empty_answer_reason` (4; exact APK `EmptyAnswerReason`), `response_doc` (5; `TailwindDoc`) |
| `orchestration.v1.TailwindDoc` | `body` (1; `Body`), `objects` (4; repeated `DocumentObject`), `type` (5; exact `ResponseType`) |
| `orchestration.v1.Body` | `content` (1; repeated `StructuralElement`), `inline_object_locations` (2; repeated `AnnotationMapEntry`) |
| `orchestration.v1.StructuralElement` | `start_index` (1; int32), `end_index` (2; int32), `paragraph` (3), `table` (5), `image` (6), `code_block` (7), `a2ui_block` (8), `thought` (9), `function_call` (10), `function_response` (11), `horizontal_rule` (12) |
| `orchestration.v1.agency.FunctionCall` / `FunctionResponse` | `name` (1), `args` (2; `TailwindStruct`) / `name` (1) |
| `orchestration.v1.Paragraph` / `ParagraphElement` / `TextRun` | `elements` (1; repeated), `paragraph_style` (2), `bullet_info` (4); `start_index` (1), `end_index` (2), `text_run` (3), `image` (4), `resource` (5); `content` (1), `text_style` (2) |
| `orchestration.v1.Table` / `TableRow` / `TableCell` | `rows` (1), `columns` (2), `table_rows` (3; repeated); `start_index` (1), `end_index` (2), `table_cells` (3; repeated); `start_index` (1), `end_index` (2), `content` (3; repeated) |
| `orchestration.v1.AnnotationMapEntry` / `ObjectId` / `Range` | `object_id` (1), `content_range` (2); `id` (1); `start_index` (2), `end_index` (3) |
| `orchestration.v1.DocumentObject` / `Citation` | `object_id` (1), `citation` (2); repeated `ranges` (4), `fragment` (5), `source_attribution` (6), `object_id` (7) |
| `orchestration.v1.TailwindDocFragment` / `CitationSource` / `SourceRevision` | `elements` (1; repeated `StructuralElement`); `ingested_source` (1); `source` (1; `SourceId`) |

The request origin and conversation-event enums come from the exhaustive checked-in
[`enums.txt`](enums.txt). Cached turns are mapped in the captured newest-first event order for each
turn: generated response (`2`) followed by user query (`1`). Every ask supplies a caller-generated
`user_message_id`; no server turn identifier is guessed. Prior-turn counting returns each
`ChatHistoryMessage.observed_event_type` unchanged to the neutral base, which counts only raw role
value `1` as a question; Android does not replace every row with a synthetic user role.
`ListChatTurns` follows unseen nonempty response field-2 page tokens through request field 6 until
the nonnegative caller limit is filled or pagination ends. Its aggregate preserves the last server
continuation token when the requested snapshot truncates the stream, and repeated tokens fail
loudly instead of hanging or double-counting history.

Stream responses are cumulative snapshots. The adapter retains the latest frame whose response
field 5 is true, never concatenates frames, and raises `ChatResponseParseError` when EOF arrives
without that final marker. Nonempty field-6 suggestion lists use last-nonempty-wins semantics across
all frames, matching the neutral Web stream contract, and preserve unknown suggestion type codes in
public `NextStepSuggestion` values. `AskResult.raw_response` is a deterministic protobuf JSON
projection of the winning final typed frame; the neutral base retains its public 1000-character
bound. Exact `EmptyAnswerReason #4` (`UNKNOWN`, `UNANSWERABLE`, or `FILTERED`) remains diagnostic
rather than adding a backend-specific `AskResult` field, but is retained in that JSON if a future
response populates it; retained live probes have not observed a nonzero reason. The final answer's
`responseDoc` is the only citation source: source identity descends
through `DocumentObject.citation.source_attribution.ingested_source.source`. Cited text preserves
the existing offset-aware paragraph/table projection; when a fragment consists only of admitted
spanless text-bearing structural variants, the shared plain-text renderer recovers code, thought,
and A2UI leaves. Answer anchors join through the proven body annotation/object IDs. Citation
source ranges at field 4 are projected as a strict min/max union of their valid pairs, independently
of the fragment-block-derived range; any invalid pair rejects the whole declared range instead of
silently narrowing it. Unrecovered scoring or other range metadata is not projected.
Citation numbering follows the raw one-based `TailwindDoc.objects` ordinal, so a preceding
non-citation object intentionally leaves a numbering gap instead of renumbering later citations.

The synthetic wire fixture
[`chat_wire.json`](../../tests/fixtures/android/chat_wire.json) (SHA-256
`674f05b27f5bfd92baac39833fd5769a91c4d85962983e88b47a354494ec52bf`) pins a request,
partial/final cumulative frames, history, and sessions at serialized-byte level. The generated
descriptors are part of the canonical cumulative
[`android_descriptor_set.pb`](../../tests/fixtures/android/android_descriptor_set.pb) fixture and
are byte-checked by the deterministic regeneration command below.

### Chat evidence boundaries

- Session/history requests leave unrecovered context fields unset. Free-form generation and cancel
  populate the shared Android `RequestContext` with Android metadata/provenance but deliberately set
  `client_type=WEB` (2): live probes show Google only cancels streams originating with that value;
  ordinary `ANDROID_APP` (3) streams continue. The separately live-proven configure mutation uses
  the ordinary captured Android context, including its provenance block.
- Chat configure/settings use the already-admitted `MutateProject` and `GetProject` paths with the
  repository-local advanced-settings request/response messages recorded in
  [`public-api-audit.md`](public-api-audit.md#chat-settings).
  A disposable Android write/read-back pinned the nested fields; partial or unknown read blocks fail
  loudly instead of being defaulted into a destructive merge. Inherited `set_mode` reaches the same
  admitted configure sender.
- Saved-from-chat note creation uses the same exact `CreateNote` method with a current-server
  overlay omitted by the older APK: repeated citation/source passages `#4`, `TailwindDoc #6`, and
  `RequestContext #7`. The citation/document closure is constructor-derived from the current
  authenticated Web bundle and live-persisted through the Android bearer as described below.
- Document tables, paragraph/list/run styles, and non-paragraph structural variants are admitted
  through the shared exact `TailwindDoc` decoder. Citation ranges field 4 is preserved on saved-note
  writes; unrecovered citation score slots remain unknown. Streamed next-step suggestions are
  admitted at exact response field 6.
- `ListChatTurns` aggregates exact request/response token pages up to the caller limit, detects
  repeated tokens, and returns the bounded protobuf snapshot. The public-shaped history decoder
  reverses those captured newest-first rows.

## Notes and sharing field ledger

The exact-package note overlay declares only these recovered fields:

| Package.Message | Field | Tag | Cardinality | Type/evidence |
|---|---|---:|---|---|
| `orchestration.v1.NoteMetadata` | `type` | 1 | singular | `NoteType`; exhaustive enum dump |
| `orchestration.v1.NoteMetadata` | `last_edit_timestamp` | 3 | singular | `google.protobuf.Timestamp`; recovered Dart symbol |
| `orchestration.v1.NoteMetadata` | `note_prompt_type` | 4 | singular | `NotePromptType`; exhaustive enum dump |
| `orchestration.v1.ProjectNote` | `id` | 1 | singular | string |
| `orchestration.v1.ProjectNote` | `content` | 2 | singular | string |
| `orchestration.v1.ProjectNote` | `metadata` | 3 | singular | `NoteMetadata` |
| `orchestration.v1.ProjectNote` | `source_passages` | 4 | repeated | current-bundle `Citation`; live response normalized the submitted row to an empty placeholder |
| `orchestration.v1.ProjectNote` | `name` | 5 | singular | string |
| `orchestration.v1.ProjectNote` | `tailwind_doc_content` | 6 | singular | current-bundle `TailwindDoc`; live response preserved the citation-rich document |
| `orchestration.v1.NoteOrStatus` | `note` | 2 | singular | `ProjectNote`; status arm #1 unrecovered/unknown |
| `orchestration.v1.GetNotesRequest` | `project_id` | 1 | singular | string; context #4 omitted |
| `orchestration.v1.GetNotesResponse` | `notes` | 1 | repeated | `NoteOrStatus` |
| `orchestration.v1.CreateNoteRequest` | `project_id`, `content`, `metadata`, repeated `source_passages`, `name`, `tailwind_doc_content`, `request_context` | 1, 2, 3, 4, 5, 6, 7 | singular/repeated | fields #1/#2/#3/#5 from APK; #4/#6/#7 current-bundle constructors plus live Android saved-note success |
| `orchestration.v1.CreateNoteResponse` | `note` | 1 | singular | `ProjectNote` |
| `orchestration.v1.NoteMutation_EditNoteMutation` | `content`, `name` | 1, 2 | singular | exact edit payload |
| `orchestration.v1.NoteMutation` | `edit_note_mutation` | 1 | singular | edit mutation |
| `orchestration.v1.MutateNoteRequest` | `project_id`, `note_id`, `mutations` | 1, 2, 3 | singular, singular, repeated | context #4 omitted |
| `orchestration.v1.MutateNoteResponse` | `note` | 1 | singular | `ProjectNote` |
| `orchestration.v1.DeleteNotesRequest` | `project_id`, `note_ids` | 1, 3 | singular, repeated | context #4 omitted |

The exact common/sharing sources plus current authenticated Web constructors admit the request
types and semantic response closure below. The repository-local response wrapper reuses the same
wire tags and changes only fields #3/#4 to `optional` so the adapter can distinguish absent from
explicit zero/false.

| Message | Field | Tag | Cardinality | Evidence boundary |
|---|---|---:|---|---|
| exact `ProjectPublicSettings` | `is_publicly_readable`, `is_discoverable` | 1, 2 | singular | pinned common source + live response/request |
| exact `GetProjectDetailsRequest` | `project_id`, `request_context` | 1, 2 | singular | pinned sharing source plus current-bundle builder; Android adapter populates both |
| exact `SharedUser` | `email`, `permission`, `profile` | 1, 2, 4 | singular | current-bundle accessors; profile carries `display_name #1`, `avatar_url #2` |
| exact and local `GetProjectDetailsResponse` | repeated `shared_users`, `public_settings` | 1, 2 | repeated, singular | current-bundle collaborator decode plus exact common settings message |
| exact/local `GetProjectDetailsResponse` | `max_individuals_share_limit` | 3 | singular / optional | exact remote field; local presence preserved |
| exact/local `GetProjectDetailsResponse` | `is_public_sharing_allowed` | 4 | singular / optional | exact remote field; local false remains distinct from absent |
| exact `ShareProjectRequest.UserPermission` | oneof `email`, `alternate_id`; `permission` | 1, 4; 3 | singular | current-bundle builders; public adapter sends email targets only |
| exact `ShareProjectRequest.ShareMessage` | `omit_message`, `message` | 1, 2 | singular | current-bundle presence-sensitive welcome-message builder |
| exact `ShareProjectRequest.ProjectToShare` | `project_id`, repeated `user_permissions`, `public_document_settings`, `share_message` | 1, 2, 3, 4 | singular/repeated | public-link and collaborator mutation branches |
| exact `ShareProjectRequest.PublicDocumentSettings` | `is_publicly_readable`, `is_discoverable` | 1, 2 | singular | pinned sharing source + successful capture |
| exact `ShareProjectRequest` | repeated `project`, `notify`, `request_context` | 1, 2, 4 | repeated/singular | pinned service plus current-bundle builders; Android adapter populates context |

Collaborator invitations/removals were intentionally not live-probed without a controlled
secondary identity. Their admission boundary is therefore the current authenticated bundle,
serialized byte-contract tests, and stateful write/read-back unit coverage, not a claim of live
side-effect proof. Populated but unnamed response tags #7/#8 remain protobuf unknown fields.
The earlier owned-copy `ShareProject` mutation returned `PERMISSION_DENIED`, but that probe targeted
the public-access service rather than the view-level field. Current `sharing.set_view_level` uses
the admitted native `MutateProject` tag-9 branch; see
[`web-compat-seam-closure.md`](web-compat-seam-closure.md#sharingset_view_level--wrong-service).

## Cross-backend mind-map classifier and the chat seam

The exact prompt enum remains one sufficient map-kind signal, but it is not necessary. Two sanitized
historical runs generated a note-backed map through Web and read the same id over Android. Both
Android rows were `NoteType.USER_WRITTEN`, `NotePromptType.NOTE_PROMPT_TYPE_UNSPECIFIED`, and a JSON
object with a top-level `children` key. Android therefore uses the union of the exact prompt signal
and the legacy JSON-object signals: `MIND_MAP`, or parsed object membership of `children` or
`nodes`. Ordinary note listing excludes that same union.

Generation is now native. The APK-exact `ActOnSourcesRequest` carries repeated `InputSource #1`,
options `#2`, free-form action `#3`, source options `#7`, `RequestContext #8`, chat-session ID
`#10`, and origin `#11`; the response wraps `AnswerResponse #1`. The current authenticated Web
constructor closes mind-map action field `#6` as action string `#1`, repeated key/value context
rows `#2`, and language `#3`. A bounded Android-bearer run sent `interactive_mindmap`, received a
nonempty JSON tree rooted at `NotebookLM Features`, and persisted that JSON through native
`CreateNote` within the same operation epoch. The exact disposable notebook was deleted. No Web
generation collaborator remains.

The selected Android adapter projects `[ProjectNote.id, ProjectNote.content]`, the two fields proved
by same-ID capture and the supported leading slots of the intentionally opaque public raw-row
contract. It does not fabricate optional Web metadata/source slots whose mobile equivalents are
unproven. `delete_mind_map` preflights this kind-specific list, sends one
non-replayed `DeleteNotes`, and polls bounded `GetNotes` reads until that map id is absent. An ordinary
note id and an already-absent map are read-only idempotent successes. The retained capture narrative is
[`resource-lifecycle-and-public-qualification.md`](resource-lifecycle-and-public-qualification.md).

The public `Note.created_at` field remains `None` because the mobile contract exposes only
`last_edit_timestamp`; copying the latter would mislabel an edit as creation. Android exact-ID
lookup reports genuine absence after `DeleteNotes`, so `get_or_none` returns `None` as required by
the public contract instead of exposing Web's private soft-delete tombstone. Raw note-backed map
rows preserve the supported `[id, content]` prefix without fabricating unproven metadata slots.

The Android chat save-note hook builds each unique chunk citation once. Its `Citation.ranges #4`,
fragment `#5`, source attribution `#6`, and object ID `#7` are sent both in CreateNote's repeated
source passages `#4` and inside the `TailwindDoc #6` `DocumentObject`; the document body stores the
citation-marker-free answer and annotation rows join anchors to those object IDs. A live Android
`CreateNote`/`GetNotes` round trip returned `ProjectNote` fields
`#1` through `#6`, document body `#1`, objects `#4`, and all citation `#4`-`#7` joins with source
identity, range, fragment, and marker anchors intact. The original marker-bearing plain content was
also preserved. The exact scratch notebook was deleted and a prefix sweep found no remainder.

## Project optional fields

The flattened [`schema.proto`](schema.proto) names `Project.premiumFeatureInfo #10` and its three
boolean leaves; a 2026-08-07 authenticated Web/Android audit independently observed the same
three-field block on the Web wire (`[true,true,false]` for free tier and `[true,true,true]` for
Pro). A current authenticated
`ListRecentlyViewedProjects` probe then found field #10 on every one of 18 returned Android project
rows, always with the exact nested fields #1/#2/#3 and boolean wire types. That independent live
corroboration admits `PremiumFeatureInfo` into the exact read closure and the public notebook
projection now carries all three capability flags. No project identifiers or titles were logged.

Project tier limits #11 remain outside the typed public notebook model but are admitted and
preserved by `notebooks.get_raw()`. `ProjectMetadata.is_public #13` and repeated
`audio_overview_artifact_ids #17` are preserved there as well. Live-proven
`Project.advanced_settings #8` is carried by the full local GetProject parser and projected into
`Notebook.chat_settings`; requested-vs-echoed notebook identity is checked before any GetProject
projection or source reconciliation. Chat sessions #12 are no longer blocked: the exact APK
schema names the existing exact-package `common.protos.ChatSession`, and a current disposable
`CreateProject` response carried Project field #12 with `chat_session_id #1`. The Android notebook
projection now returns that session and volunteers it once to the neutral first-ask workflow. The
scratch project was deleted by exact ID and a title-prefix sweep verified cleanup.

## Source-operation admission

The exact-package `supported.proto` snapshot above also supplies the orchestration request/message
FQNs, field tags/cardinality, import boundary, and service method manifest for
`AddTentativeSources`, `AddSources`, `DeleteSources`, `GenerateDocumentGuides`, and `LoadSource`.
The source contract copies only the fields
its builders and codecs reach into
`google/internal/labs/tailwind/orchestration/v1/sources.proto`; it imports the read types rather
than redeclaring `Source` or `SourceId`. The message overlay intentionally declares no service. The
cumulative service imports those five exact signatures plus the web-derived `MutateSource` and
`RetrieveRelevantChunks` signatures and the current-bundle-derived
`CheckSourceFreshness`/`RefreshSource` signatures.
Blutter's generated-client binding proves `DeleteSources` returns
`google.protobuf.Empty`. Runtime dispatch stays on `AndroidSession`'s generic typed callable.

| Full method | Request FQN | Response FQN | Replay |
|---|---|---|---|
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/AddTentativeSources` | `.google.internal.labs.tailwind.orchestration.v1.AddTentativeSourcesRequest` | `.google.internal.labs.tailwind.orchestration.v1.AddTentativeSourcesResponse` | never |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/AddSources` | `.google.internal.labs.tailwind.orchestration.v1.AddSourcesRequest` | `.google.internal.labs.tailwind.orchestration.v1.AddSourcesResponse` | never |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/DeleteSources` | `.google.internal.labs.tailwind.orchestration.v1.DeleteSourcesRequest` | `.google.protobuf.Empty` | never; exact signature |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/MutateSource` | generated web-derived `.google.internal.labs.tailwind.orchestration.v1.MutateSourceRequest` | generated web-derived `.google.internal.labs.tailwind.orchestration.v1.MutateSourceResponse` | never; response wraps `Source #1` |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/CheckSourceFreshness` | generated web-derived `.google.internal.labs.tailwind.orchestration.v1.CheckSourceFreshnessRequest` | generated web-derived `.google.internal.labs.tailwind.orchestration.v1.CheckSourceFreshnessResponse` | safe read; valid-resource Android success |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/RefreshSource` | generated current-bundle-derived `.google.internal.labs.tailwind.orchestration.v1.RefreshSourceRequest` | generated current-bundle-derived `.google.internal.labs.tailwind.orchestration.v1.RefreshSourceResponse` | never; valid stale Drive source refreshed successfully through Android bearer |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GenerateDocumentGuides` | `.google.internal.labs.tailwind.orchestration.v1.GenerateDocumentGuidesRequest` | `.google.internal.labs.tailwind.orchestration.v1.GenerateDocumentGuidesResponse` | safe read |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/LoadSource` | `.google.internal.labs.tailwind.orchestration.v1.LoadSourceRequest` | `.google.internal.labs.tailwind.orchestration.v1.LoadSourceResponse` | safe read |
| `/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/RetrieveRelevantChunks` | generated web-derived `.google.internal.labs.tailwind.orchestration.v1.RetrieveRelevantChunksRequest` | generated web-derived `.google.internal.labs.tailwind.orchestration.v1.RetrieveRelevantChunksResponse` | safe read; unfiltered and source-filtered Android success; [wire evidence](source-search-evidence.md#wire-layout) |

### Source field ledger

| Package.Message | Fields admitted | Evidence/use |
|---|---|---|
| `orchestration.v1.InputSource` | `source_id #1` | exact closure; guide request/correlation |
| `orchestration.v1.Snippet` | `text_snippet #1` | exact closure; guide summary |
| `orchestration.v1.MainIdeas` | repeated `text_ideas #1` | exact closure; guide keywords |
| `orchestration.v1.DocumentGuide` | `source #1` (optional on the wire), `snippet #2`, `main_ideas #3` | exact closure; guide projection keyed on an echo the server may omit — see [Document-guide source echo](#document-guide-source-echo) |
| `orchestration.v1.GenerateDocumentGuidesRequest/Response` | repeated `sources #1` / repeated `guides #1` | exact method closure |
| `orchestration.v1.TentativeSourceMetadata` | `name #1` | exact closure; bijective correlation key |
| `orchestration.v1.AddTentativeSourcesRequest` | repeated metadata `#1`, `project_id #2`, `request_context #3`, `provenance #4` | exact closure; URL builders leave #3/#4 absent, file-upload registration populates them |
| `orchestration.v1.AddTentativeSourcesResponse` | repeated `tentative_sources #1` | exact wrapper |
| `orchestration.v1.WebContent` | `url #1` | exact closure; outbound URL bytes |
| `orchestration.v1.UserContent` | `web_content #3`, `tentative_source_id #9` | exact closure; URL commit branch |
| `orchestration.v1.AddSourcesRequest/Response` | repeated `user_content #1`, `project_id #2` / repeated `sources #1` | exact method closure |
| `orchestration.v1.DeleteSourcesRequest` | repeated `source_ids #1` | exact method closure |
| `orchestration.v1.CheckSourceFreshnessRequest/Response` | request `source_id #2`, `request_context #3`; response `source_freshness #1`, whose state is optional bool `#2` and ID `#3` | current-bundle constructor/accessor plus valid-resource Android read |
| `orchestration.v1.RefreshSourceRequest/Response` | request `source_id #2`, `request_context #3`; response `source #1` | current-bundle constructor/accessor plus valid stale-Google-Doc Android refresh |
| `orchestration.v1.PlainTextSourceContent` | `header #1`, `body #2` | exact response closure; flat text uses body |
| `orchestration.v1.LoadSourceRequest/Response` | `source_id #1` / `source #1`, `plain_text #2`, `markdown_string #3`, `TailwindDoc #4` | exact method closure; current live responses used only `source #1` plus `TailwindDoc #4`, decoded through the local response overlay |
| `orchestration.v1.RetrieveRelevantChunksRequest` | `project_id #1`, `query #2`, options `#4`, source-id filter `#5` | Web layout plus unfiltered and filtered native Android calls |
| `orchestration.v1.RetrieveRelevantChunksResponse` | repeated source groups `#1`; group `source_id #1`, chunks `#2`; chunk content/rank/spans `#1/#2/#3`; span start/end `#2/#3` | Web and Android live replies; [full evidence](source-search-evidence.md#wire-layout) |

### Document-guide source echo

`DocumentGuide.source #1` is **optional in practice**, so requested-vs-echoed identity on
`GenerateDocumentGuides` is a *leak* check, not a *presence* check. Two current authenticated probes
(2026-08-31, issue #2276 then #2278) establish what governs it:

| Call | `DocumentGuide` fields on the wire | Echo |
|---|---|---|
| first response for a source | `#1`, `#2`, `#3`, empty `#4` | `#1.source_id.id` equals the requested id |
| every repeat call | `#2`, `#3`, empty `#4` — **no `#1`** | absent |

The second probe called `GenerateDocumentGuides` three times in a row for each of three
never-before-read sources: every one returned the label on call 1 and omitted it on calls 2 and 3,
with byte-identical summary lengths (930 / 1062 / 798 chars). The unlabelled form is therefore the
steady state, and the labelled form is the exception.

**Source type does not predict it**, contrary to the first reading recorded here. The initial probe
compared three URL sources in a long-read notebook against three pasted-text sources in a fresh one
and attributed the split to type; re-probing the same text sources hours later returned them
*unlabelled*, and a URL source read for the first time returned *labelled*. Type, age and notebook
were all confounded with call ordinal. Both shapes are now pinned against recorded bytes in
`tests/cassettes/android/source_lifecycle_recorded.grpc.json` (interactions 10 and 11: the same
source, read twice).

The repeat responses omit field `#1` from the serialized message entirely; the only other field
present is a zero-length `#4`, which carries no identifier. That rules out the competing reading in
which the server relocates the label into an unmodelled `InputSource` branch that our parser would
report as `HasField("source_id") == False` — there is no such branch on the wire to miss. The same
probe found that a two-source request is rejected with `INVALID_ARGUMENT`, so the endpoint is
single-source and a lone unlabelled guide can only describe the source that was asked about.

`sources.get_guide` therefore accepts a sole unlabelled guide, keeps the hard failure for a
*populated and different* echo, and still requires an exact match once more than one guide is
returned — the same `if echoed_id and echoed_id != source_id` convention already used by
`refresh` and `check_freshness`. The rejection paths carry the requested id, every observed
echo, the guide count, and each guide's field tags, because the original error carried none of
them and could not be diagnosed from CI logs. The tags are reported instead of a wire-byte preview:
an unlabelled guide's payload begins with `#2 snippet`, so any prefix would carry the start of a
model-written summary of the user's source into an error string that `NOTEBOOKLM_DEBUG=1` prints
untruncated.

The probe also corroborates the `main_ideas #3` projection against the alternative reading of the
captured app traffic (which annotates response `#3` as "(inferred) suggested questions"): every
probed source returned five short noun phrases — `"Model Context Protocol"`, `"Centralised
Authentication"`, `"Python Programming"` — none of them interrogative.

`LoadSource` echoed the requested id on every probed source and call, so its sibling check is not
affected; its unlabelled branch nonetheless defers to `decode_source`, which reports
missing-identifier drift instead of claiming the source does not exist.

### Derived-read existence policing

`get_guide` and `check_freshness` are **derived reads** under
[ADR-0019](../adr/0019-error-and-return-contract.md): they do not police parent existence. Both
Android RPCs are notebook-agnostic — they carry a source id and no project id — and a live probe
(2026-08-31, issue #2278) confirmed the web backend is too: asked for a source of notebook B while
naming notebook A, both backends returned B's guide. The adapter previously ran a `GetProject`
pre-flight that raised `SourceNotFoundError`, which made the `notebook_id` argument meaningful on
Android alone while diverging from web and costing a round-trip per call.

The same probe pinned the target behaviour for a nonexistent source id:

| | web | Android raw RPC | Android adapter (now) |
|---|---|---|---|
| `get_guide` | `SourceGuide("", ())` | `GenerateDocumentGuides` → `NOT_FOUND` | `SourceGuide("", ())` |
| `check_freshness` | `True` | `CheckSourceFreshness` → **empty response** | `True` |

`check_freshness` needed no mapping: the backend answers a bogus id with an empty
`CheckSourceFreshness` response, which the existing decode already reads as fresh. `refresh` keeps
its ownership check — mutating a missing source must still raise.

The admitted source overlay also carries the public text, YouTube, and Drive-reference branches used
by `add_text`, YouTube `add_url`, and `add_drive`; their exact fields are exercised by focused
request-contract tests and live disposable sources. The repository-local
`WireLoadSourceResponse` retains the official response's exact field types while avoiding the
`sources.proto`/`chat.proto` import cycle. Redacted live probes of both a pasted-text source and a
Drive-hosted text file returned top-level fields `#1/#4` and omitted `#2/#3`; field `#4` parsed as
the existing exact-package `TailwindDoc`, contained the synthetic marker, and now backs both the
legacy flat-text and Markdown public renderings. Both sources reached `READY`, chat independently
reproduced each marker, and every scratch notebook, Drive file, and Android download temp directory
was removed. The shared decoder mirrors the public Web projection for paragraph run styling,
heading/list metadata, table-cell ranges, and recursively flattened table spans. Code, thought,
image, A2UI, horizontal-rule, and unknown blocks remain explicit offset-holding kinds in
`StructuredDocument`; the separate flat renderer includes code, thought, and A2UI payload text,
while omitting image URLs and resource IDs because the admitted messages expose no human-readable
label for them. Unused adjacent branches remain omitted because flattened recovery alone is not a
reachability reason.

## Generic file-upload admission

The upload closure does not infer packages from the flattened message list. The independently
recovered Dart library boundaries in [`schema.proto`](schema.proto) name
`google.internal.labs.tailwind.orchestration.v1/labs_tailwind_orchestration_service.pb.dart`,
`labs.language.tailwind.common.protos/metadata.pb.dart`, and
`labs.language.tailwind.common.protos/provenance.pb.dart`. Those package/library identities,
together with the exhaustive nested enum inventory in [`enums.txt`](enums.txt), admit the exact
FQNs below. Live PDF and text-file probes prove the declared fields are reachable in successful
requests. No capability, execution-mode, app-API, or unused provenance field is copied.

| Package.Message | Fields/enums admitted | Evidence/use |
|---|---|---|
| `common.protos.ClientInfo` | nested `ApplicationPlatform { UNSPECIFIED=0, NATIVE=2 }`; nested `Device { UNSPECIFIED=0, MOBILE_ANDROID=1 }`; `application_platform #1`, `device #2`, `application_version #3` | exact Dart library boundary + exhaustive enum dump; both registration and start JSON provenance |
| `common.protos.Provenance` | nested `OriginProductType { UNSPECIFIED=0, GOOGLE_NOTEBOOKLM=1 }`; `origin_product_type #1`, `client_info #11` | exact Dart library boundary + successful live body |
| `common.protos.ClientType` | `UNKNOWN=0`, `ANDROID_APP=3` | exact enum inventory; registration context |
| `common.protos.ClientMetadata` | `client_version #1` | exact Dart library boundary; captured app version |
| `common.protos.RequestContext` | `client_type #1`, `client_metadata #2`, `provenance #4` | exact Dart library boundary; unreachable fields #3/#5/#6 omitted |
| `orchestration.v1.UploadFileRequest` | `project_id #3`, `request_context #4`, `source_id #5`, `provenance #6` | exact orchestration Dart library boundary + structurally matched successful start JSON |

`UploadFileRequest` is used as a deterministic binary descriptor/field-number gate. Runtime JSON
is an explicit captured-field builder, not a generic protobuf-to-dictionary layer. The only upload
registration route remains the already admitted `AddTentativeSources` unary method, always
non-replayed. Scotty start/finalize are HTTP and add no guessed gRPC service declarations.

`sources.add_drive_file` is native composition rather than an additional gRPC method. The Android
OAuth bearer reads fixed-origin Drive v3 metadata and `alt=media`, applies the public upload type
and 200 MiB bounds, streams through a permission-restricted temporary file, then enters the same
Android tentative-registration/Scotty pipeline. Native Google Workspace documents are rejected
with `add_drive` guidance because they are references, not downloadable upload files. A bounded
live text-file run reached READY, preserved the title, deleted the Drive file by exact ID with HTTP
204, and deleted its disposable notebook.

### Web-derived MutateSource closure

The valid-resource replay in
[`grpc-capability-and-signature-evidence.md`](grpc-capability-and-signature-evidence.md#mutatesource)
(SHA-256 `00091066e51b76c8e072100da8935de6b39cca30a8e7142ce11fb7a07b2ae15c`)
proves the method path and request bytes — `SourceId #2`, repeated mutation `#3`, change-title
message `#1`, title `#1`. The current web bundle additionally proves request context `#4` and a
dedicated response wrapper containing exact-package `Source #1`. The source adapter now uses generated
`MutateSourceRequest`/`MutateSourceResponse`; their conventional type names remain explicitly
marked as web-derived inference rather than APK extraction.
`SourceMutation` is a oneof: the implemented title branch is `#1`; live branches `#2/#3` remain
unrecovered and undeclared rather than being mislabeled or reserved.

## Organization admission

The archived exact-package `supported.proto` pinned above independently establishes the remote
`GetLabelsRequest`, `GetLabelsResponse`, `LabelAndSources`, and service signature. It declares
response field `#2` as another repeated `LabelAndSources`; authenticated Android-bearer rows instead
prove that field contains notebook collections with the live-derived shape below. The minimal
`organization.proto` therefore preserves the exact request/response FQNs, service binding, tags, and
cardinality while explicitly correcting field `#2`'s message type from live evidence. `GetLabels`
remains an exact generated-stub method, not a signature exception, but its field-`#2` type is not
claimed as a literal copy of the archived descriptor.

| Evidence-qualified message | Admitted fields |
|---|---|
| `GetLabelsRequest` | `project_id #2`, `label_type #3` |
| `LabelAndSources` | `label #1`, repeated `SourceId source_ids #2`, `label_id #3`, `emoji #4` |
| `GetLabelsResponse` | repeated `label_and_sources #1` (archived exact); repeated `NotebookCollection notebook_collections #2` (live-derived type correction; archive declares `LabelAndSources`) |
| `NotebookCollection` | live-derived `name #1`, repeated string `notebook_ids #2`, `id #3`, `emoji #4` |
| `CreateLabelResponse` | repeated `label_and_sources #2`, repeated `notebook_collections #3` |

Live collection rows reuse outer record field `#2` but encode each notebook UUID as bare UTF-8,
whereas source-label rows encode a nested `SourceId`. A populated collection can therefore not be
safely projected by treating every member as the exact `SourceId` declaration. Runtime reads use
the visibly repository-local `GetLabelsWireResponse` with `repeated bytes member_ids #2`, then the
codec applies the evidenced per-resource interpretation and canonical-UUID validation. This is a
wire-decoder overlay only; it does not weaken the independently evidenced service signature.

The valid-resource reports admit the manual organization write branches and the automatic-label
create branch below.
Current-web registration and constructor tracing adds conventional request/response type names and
proves distinct nested constructors for create properties, mutate properties, and all four member
operations. All three write requests populate the observed request context at `#1`. `LabelMutation`
is a oneof for the admitted branches `#1`-`#5`; live branches `#6/#7` remain unrecovered and
undeclared. Those structural/type-name inferences are generated and explicitly manifested.
`CreateLabelResponse` admits proven source-label rows at `#2`. A later authenticated collection
create pins its collection rows at top-level `#3`, with name `#1`, repeated raw notebook UUID
strings `#2`, collection UUID `#3`, and emoji `#4`. `CreateLabelRequest` is a oneof: `auto_create #5` is distinct from
`manual_create #6`. `AutoCreateLabel.regenerate_all #1` is optional, so absent, explicit false, and
explicit true remain three distinct wire modes. Mutate/delete use dedicated response constructors
with no fields retained by the bundle, so their generated partial parsers admit no fields; they are
not labeled `google.protobuf.Empty`.

| Method | Generated web-derived populated wire | Runtime rule |
|---|---|---|
| `CreateLabel` | request `project_id #2`; `auto_create #5` with optional regenerate-all `#1`, or `manual_create #6` containing properties `#1`, sources `#2`, notebooks `#3`; collection discriminator `#7 = 3`; response labels `#2`, collections `#3` | non-replayed write; manual label and collection creates require exactly one canonical row in the direct response, avoiding a racy `GetLabels` diff; auto create uses canonical read-back for the complete post-write set |
| `MutateLabel` | resource `#3`, repeated operation `#4`; label project `#2`; collection discriminator `#5 = 3`; property `#1`; member add/remove variants `#2`-`#5` | one member per non-replayed RPC, one final read-back, partial/non-atomic failure across members |
| `DeleteLabels` | label project `#2`, repeated IDs `#3`; collection discriminator `#4 = 3` | preflight absent IDs as idempotent no-ops, one non-replayed batch delete, then absence read-back |

Every multi-call public adapter workflow holds one outer operation lease and passes its epoch to
each organization unary call. The adapters and codecs import generated protobuf modules only from
lazy helpers, preserving dependency-free construction and deferring a missing Android extra to
async open. `AndroidCollectionsAPI` implements all nine public collection methods;
`AndroidLabelsAPI` implements all twelve public label methods. Live scratch probes proved automatic
mode false preserves existing manual labels while labeling only unlabeled sources, mode true
replaces all label IDs, and absent/default is distinct and preserves the manual label in an empty
source set. Every scratch notebook was deleted.

## Deterministic toolchain

| Component | Exact value |
|---|---|
| `grpcio-tools` | `1.76.0` |
| embedded `protoc` | `libprotoc 31.1` |
| protobuf Python runtime | `6.33.5` |
| gRPC Python runtime | `1.76.0` |
| flags | both proto roots via `-I`, `--include_imports`, `--descriptor_set_out`, `--python_out`, `--grpc_python_out`; sorted input list |

Run `python scripts/regenerate_android_protos.py --check` in the locked dev environment. The check
compiles the cumulative read-account message and exact-service closure into a temporary directory,
performs the repository-local Python import relocation for every exact package root, and
byte-compares the canonical descriptor set plus the complete generated module tree. Use `--write`
only when the reviewed proto sources and pinned toolchain intentionally change.
`notes_sharing_request_wires.json` independently pins every populated
note and sharing request byte sequence.
