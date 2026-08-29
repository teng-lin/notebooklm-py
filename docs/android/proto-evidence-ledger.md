# Android protobuf evidence ledger

**Status:** admitted B1 read closure plus B2 notebook wire overlay

**Evidence snapshot:** 2026-08-27

**Scope:** B1 notebook/source reads and the B2 notebook operations listed below

This ledger is the admission boundary for `src/notebooklm/_android/proto_src/`. The recovered
[`schema.proto`](schema.proto) is Dart-AOT evidence, not a compile input: it flattened several
libraries and contains duplicate package-local persistence declarations. The checked-in B1 proto
sources instead preserve the exact wire packages from the descriptor/Dart library boundary and
copy only the fields below. Missing fields remain protobuf unknown fields; they are not filled from
plausible-looking flattened declarations.

## Evidence input identities

The external exact-package snapshot was reviewed, reduced to B1, and then made self-contained by
this ledger, the checked-in proto sources, descriptor set, and synthetic fixtures. Hashes prevent a
later local checkout from silently changing what was admitted.

| Evidence input | SHA-256 | Role |
|---|---|---|
| exact-package orchestration `supported.proto` | `829c4ee871fd66421ee098fa266793ec68773e625ff005cc519b2c0f7c191ae9` | service/message FQNs, package, tags, cardinality, import origin |
| exact-package `source_settings.proto` | `becd695c4281e23064c16fc1441c61117e5dc2a44c52cadf44af9e31c7cb8b18` | separate settings package, fields #2/#4, complete enums |
| [`schema.proto`](schema.proto) | `aa9f49d302ff9a64cc16d08b2f2f9031f77a348b3707dd98df37be91a67355ec` | flattened Dart recovery used to identify gaps, never as a compile input; hash includes the curated `docs/android/` path comments |
| [`enums.txt`](enums.txt) | `8c8137c1842d07b54ba9e52feeea7c3ce09246415c26d964d17bec68eee228bc` | exhaustive enum names and integers |
| exact method manifest | `c2cf4bf2e6cdefd35232f01572070fbe07d11ef9bad99b556f76b5e3748f38a3` | full method paths, request/response FQNs, unary cardinality |

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
