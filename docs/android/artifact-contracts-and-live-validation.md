# Android artifact contracts and live validation

**Status:** Artifact generation, mutation, local decoding, and the admitted transfer closure are
implemented from static APK evidence, current web-bundle contracts, and disposable-resource live
probes. Retry dispatch is wire- and handler-proven but still lacks an accepted failed-row replay;
report-to-Google-Docs export is live-proven, while Sheets and literal-content variants remain
web-derived.

**Validation date:** 2026-08-29

This document consolidates four sanitized evidence reports. It distinguishes exact recovered
Google descriptors from repository-local wire-equivalent projections and from conventional names
inferred from the current web bundle.

## Source-report provenance

The hashes below identify the complete reports immediately before consolidation. Their distinct
probe results are retained as sections in this document.

| Original report | Validated | Original SHA-256 | Distinct evidence retained here |
|---|---|---|---|
| `artifact-gap-live-validation-2026-08-29.md` | 2026-08-29 | `a643e7b324dc69293f44251f3919f35c6875ffb488dfebffcfdb365430a0b8e2` | APK artifact inventory, exact representations and user state, slide derivation, interactive mind map, report decoding, and media transfer boundary |
| `agent-data-table-2026-08-29.md` | 2026-08-29 | `d9b2e424fdc1425eae8471b7382851f35e54173a38d443e953185dc5d5aa89ff` | field-19 table overlay, live creation/read-back, and Android/Web CSV parity |
| `agent-media-note-artifact-generation-evidence-2026-08-29.md` | 2026-08-29 | `7a483f43e0f8eeb258de639c0866c4efb062a2a4f2e5d55391287800cc187640` | audio and infographic enum recovery, cinematic identity, concept-report acceptance, and note-backed compatibility boundary |
| `agent-retry-export-evidence-2026-08-29.md` | 2026-08-29 | `ee0bd8aaff78e2704b909a2a3e545e986f29c4d182dd96009450e480896739ec` | retry/export request and response contracts, failed-row precondition evidence, report-to-Docs verification, and exact Drive cleanup |

## Evidence identity and safety boundary

- Package: `com.google.android.apps.labs.language.tailwind`
- Official APK version: `1.46.7.940945420`
- APK AOT SHA-256:
  `082d75e36eb03aea7ea5a8c252029c48b964177311ca4ebac6392814b8e6f81f`
- Static evidence: the recovered APK AOT output and the repository's reduced protobuf evidence.
- Dynamic evidence: Android-bearer gRPC probes against uniquely named disposable notebook copies;
  retry/export signatures were also checked against the authenticated current web bundle.

Authentication read a durable credential from an isolated temporary profile and exchanged it in
memory. Credentials, account identity, notebook/source/artifact IDs, titles, prompts, content,
capability URLs, and exported-document identifiers were neither logged nor persisted. Each probe
group deleted its scratch notebooks in `finally`, and exact-prefix or exact-title absence checks
confirmed cleanup. The exported Google Doc was separately read back and deleted by exact ID before
its scratch notebook was removed. The emulator used by the umbrella validation was stopped after
the final zero-resource sweep.

## Artifact inventory and representations

Android `ListArtifacts` and exact per-ID `GetArtifact` succeeded for eight copied Studio families:
audio, tailored report, video, quiz, flashcards, infographic, slides, and data table. Full
`GetArtifact` responses, rather than list summaries, carried the large representation payloads:
approximately 1.35 MB of flashcard HTML, 2.37 MB of quiz HTML, the report `TailwindDoc`, media
endpoints, and both slide PDF and PPTX URLs.

The exact APK/recovered fields admitted by this pass are:

- `AppArtifact.app_html #1`, `TemplatizedApp.app_data #1`, and live interactive
  `AppArtifact.mind_map_json #4`;
- `TailoredReportArtifact.report_doc #3`;
- `SlidesArtifact.pdf_download_url #4` and `pptx_download_url #5`;
- `FileArtifact.file_name/mime_type/preview/download #1/#2/#3/#4`;
- audio `media_urls #6`, video `media_urls #5`, and `MediaStreamingUrl.url/type #1/#2`;
- `Artifact.artifact_user_state #18`, with `ArtifactState` audio/video/app/scheduled branches
  `#1/#2/#3/#4`; audio playback is `AudioOverviewState.playback_position #1`, and app progress is
  the exact `AppArtifactState.app_state #1` protobuf `Struct`.

The decoder projects the exact user-state closure instead of dropping it: audio playback becomes
`AudioArtifactUserState`, recognized flashcard `Struct` keys become
`FlashcardArtifactUserState`, and any other populated state is retained as
`UnknownArtifactUserState` rather than silently discarded.

The live report contained 62 structural elements: 54 paragraphs, one table, and seven horizontal
rules, plus 99 text runs, 46 paragraph-style blocks, and 35 bullet blocks. The renderer therefore
admits the exact paragraph/table/rule/style/bullet closure instead of flattening only paragraphs.

## Generation contract summary

| Surface | Wire contract | Live/static result |
|---|---|---|
| Audio format | `AudioOverviewGenerationOptions`, varint `#7`; `1=DEEP_DIVE`, `2=BRIEF`, `3=CRITIQUE`, `4=DEBATE` | Codes 2, 3, and 4 returned OK and were echoed |
| Infographic detail | `InfographicGenerationOptions`, varint `#5`; `1=CONCISE`, `2=STANDARD`, `3=DETAILED` | Code 3 returned OK and was echoed |
| Cinematic video | `ExplainerVideoGenerationOptions.template_format #5 = 3`; style omitted | APK UI/converter identity is exact; live dispatch reached the handler but quota was exhausted |
| Concept explanation | Flexible `TailoredReportArtifactGenerationOptions` string fields | Preset returned OK and echoed `Concept Explanation` |
| Data table | `Artifact #19`, document `#1`, generation options `#2` | New table reached READY; exact document rendered to Web-equivalent CSV |
| Interactive mind map | `AppArtifactGenerationOptions.app_type=4`, prompt `#3`, language `#4` | Reached READY; exact JSON tree decoded |
| Note-backed mind map | `ActOnSourcesRequest.sources #1`, mind-map action `#6`, request context `#8`; then `CreateNote` | Android bearer returned JSON and native note persistence succeeded |

## Audio overview format

The recovered official message proves these named fields:

```proto
message AudioOverviewGenerationOptions {
  string episode_focus = 1;
  EpisodeLength episode_length = 2;
  repeated SourceId source_ids = 4;
  string language_code = 5;
}
```

The server accepts and echoes an additional varint field at tag 7. The web wire registry supplies
the semantic values 1 through 4. Authenticated Android `CreateArtifact` probes independently
exercised codes 2, 3, and 4; all three returned gRPC OK, artifact type 1 in `PROCESSING`, and the
same field-7 value. A safe wire-equivalent declaration is therefore:

```proto
message WireAudioOverviewGenerationOptionsProjection {
  int32 format = 7;
}
```

The nested enum's official protobuf name/FQN is absent from the APK descriptor, but enum names do
not affect protobuf wire compatibility. The tag, wire type, public values, and live behavior are
independently proven.

## Infographic detail level

The recovered official message proves prompt field 1, language field 2, aspect field 4, and style
field 6. The server accepts and echoes detail level as a varint at tag 5. The web registry supplies
the exact semantic codes, and an authenticated Android request proved `DETAILED=3` end to end:

```proto
message WireInfographicGenerationOptionsProjection {
  int32 detail_level = 5;
}
```

The nested enum's official symbolic FQN is absent, but the tag, wire type, public-to-wire values,
and live behavior are proven.

## Cinematic video

The public `VideoFormat.CINEMATIC` value 3 is correct. The official APK contains two views of the
same enum value:

- The application-model enum value/index 3 is internally named `breakdown`.
- `templateFormatDisplayTitle` maps that exact branch to the visible title `Cinematic`.
- `TailwindRpcService.createExplainerVideoArtifact` converts it to
  `VideoOverviewTemplateFormat` protobuf value 3.
- The same converter omits `video_overview_style` and `style_prompt` for this value, matching the
  cinematic UI contract.

The recovered proto name `TEMPLATE_FORMAT_BREAKDOWN = 3` is therefore a stale internal label, not
evidence that cinematic is unsupported. The implementation emits `template_format = 3` with style
omitted. An authenticated code-3 creation attempt reached the generation handler and returned gRPC
status 8, `RESOURCE_EXHAUSTED`, because the account's video quota was exhausted. It did not return
`INVALID_ARGUMENT` or an unknown-field error. The APK UI-to-converter identity establishes the
mapping despite the quota-limited live result.

## Concept explanation report

Mobile report generation uses a free-form string contract, not a closed report-format enum:

```proto
message TailoredReportArtifactGenerationOptions {
  string type = 1;
  string description = 2;
  repeated SourceId source_ids = 4;
  string language_code = 5;
  string document_directive = 6;
}
```

The following deterministic SDK preset was accepted by Android `CreateArtifact`, returned artifact
type 2 in `PROCESSING`, and echoed the type string:

- Type: `Concept Explanation`
- Description: `Clear explanations of key concepts`
- Directive: `Explain the key concepts from the provided sources clearly and comprehensively. Define important terms, connect related ideas, use examples where helpful, and address common misconceptions.`

This preset is SDK policy expressed through the exact flexible mobile contract; it is not a
recovered hidden server enum.

## Data-table wire contract and CSV projection

The ready type-9 artifact and a newly created type-9 artifact both use this envelope:

```proto
// Local names below are conventional; their tags and referenced TailwindDoc FQN are proven.
message TableArtifactGenerationOptions {
  string prompt = 1;
  string language_code = 2;
}

message TableArtifact {
  google.internal.labs.tailwind.orchestration.v1.TailwindDoc document = 1;
  TableArtifactGenerationOptions generation_options = 2;
}

message Artifact {
  // Existing fields omitted.
  TableArtifact table = 19;
}
```

`TableArtifact` and `TableArtifactGenerationOptions` are deliberately local, wire-equivalent
names. The official APK's `Artifact` builder registers tags 18 and 22 but calls `addUnused()` for
tag 19, so the inspected APK contains no generated Dart table-payload class or recoverable nested
FQN. Protobuf message names are not transmitted on this unary path; field numbers and wire types
are sufficient. The document itself is not guessed: field `#1` parses as the already recovered
exact FQN `google.internal.labs.tailwind.orchestration.v1.TailwindDoc`, including its exact
`StructuralElement.table #5 -> Table.table_rows #3 -> TableRow.table_cells #3` closure.

For creation, document field `#1` is omitted and only field `#2` generation options are sent.
Ordered source IDs remain in top-level `Artifact.sources #4`, matching the other creation families.

### Static table evidence

The relevant AOT builder was inspected by locating `Artifact` in the recovered
`artifacts.pb.dart`; tag 19 is unused in that generated client. The reduced protobuf evidence pins
the exact-package `TailwindDoc`, `StructuralElement`, `Table`, `TableRow`, and `TableCell` closure
already compiled into this repository. These checks prove why the outer table names remain local
while the document closure is exact.

The original checks can be repeated against any local extraction without relying on a user-specific
path:

```bash
sed -n '1048,1450p' \
  <blutter-output>/asm/google.internal.labs.tailwind.orchestration.v1/artifacts.pb.dart

rg -n 'message (TailwindDoc|StructuralElement|Table|TableRow|TableCell)' \
  <mobile-evidence>/proto/google/internal/labs/tailwind/orchestration/v1/supported.proto
```

### Live table generation

The bounded probe constructed these request bytes without adding an unproven descriptor name:

```text
CreateArtifactRequest:
  project_id #2
  artifact #3:
    type #3 = ARTIFACT_TYPE_TABLE (9)
    repeated sources #4
    table #19:
      generation_options #2:
        prompt #1
        language_code #2
```

The mobile bearer endpoint admitted the request, returned a type-9 artifact whose response retained
field `#19`, and the artifact reached `ARTIFACT_STATUS_READY` on the third three-second polling
attempt. Its completed field `#19/#1` contained one populated `TailwindDoc` table node. This
supersedes the earlier bare-request failure: the bare request omitted the mandatory field-19
options envelope; it did not prove that the backend lacked table generation.

### CSV download parity

The copied ready table decoded to one `TailwindDoc` table with 11 rows and 6 columns. The Android
projection:

1. Requires exactly one top-level `StructuralElement` with `table` populated.
2. Preserves `Table.table_rows` and every row's `table_cells` order.
3. Concatenates `ParagraphElement.text_run.content` in wire order for each cell, retaining
   code-block content as a defensive admitted variant.
4. Treats the first row as CSV headers and all remaining rows as data.
5. Writes with the public API's existing UTF-8-with-BOM CSV convention.

The resulting 11-by-6 matrix matched the Web backend's CSV output for the same copied artifact
exactly after normal CSV/BOM decoding. Android `download_data_table` therefore renders the exact
mobile `TailwindDoc` locally without a remote transfer URL or Web positional-row fallback. It fails
closed on zero or multiple top-level table nodes, missing header cells, malformed cells, and
non-rectangular rows.

The repository-local creation overlay duplicates ordered source IDs in `Artifact.sources`, sets
only `table.generation_options` on creation, and decodes the public generation prompt from
`table.generation_options.prompt`. Typed and exact-protobuf prefetch follow the same bounded policy
as the other Android artifact downloads.

There is no remaining protocol or live-service blocker for Android data-table generation or CSV
download. Google Sheets export is a distinct `ExportToDrive` path; its wire contract is admitted
below, but a successful Sheets export was not established by the table probe.

## Slide derivation

`DeriveArtifact` was invoked with the APK-exact request: `RequestContext #1`, original artifact ID
`#2`, and `SlidesDerivationOptions #3` containing one slide-index/edit instruction. It returned OK
with a new type-8 artifact, progressed from `INITIALIZED` to `READY`, and exposed both PDF and PPTX
representations. This promotes `artifacts.revise_slide` from schema-only to `mobile_live`.

## Mind maps: current Android and legacy note-backed

The official APK's current mind-map customization collects sources, prompt, and language, selects
the mind-map application type, and calls `createAppArtifact`. The exact request is an artifact of
type APP with:

```proto
AppArtifactGenerationOptions {
  app_type: 4
  free_text_steering_prompt: <instructions>  // field 3
  language_code: <language>                 // field 4
}
```

An Android `CreateArtifact` request using outer artifact type 4 and app type 4 reached `READY`.
`GetArtifact` field `AppArtifact #4` was direct UTF-8 JSON with the recursive `{name, children}`
grammar. The captured tree contained 67 valid nodes, maximum depth 3, and no unexpected node shape.
Interactive generation, tree read, and JSON download are therefore admitted.

The legacy Web note-backed generation path uses Batchexecute `GENERATE_MIND_MAP (yyryJe)` with
this positional payload:

```text
[
  source_ids_nested, null, null, null, null,
  ["interactive_mindmap", [["[CONTEXT]", instructions_or_empty]], language],
  null,
  [2, null, [1]]
]
```

The APK establishes the exact `ActOnSourcesRequest` and response FQNs and the request fields for
sources `#1`, options `#2`, free-form action `#3`, source options `#7`, request context `#8`, chat
session `#10`, and origin `#11`. The current Web constructor additionally closes the action union:
field `#6` is a message with action string `#1`, repeated key/value context rows `#2`, and language
`#3`. Its product call site sets action `interactive_mindmap`, context key `[CONTEXT]`, the caller's
instructions as the value, and the requested language. This is a protobuf field, not merely an
array-index guess.

A bounded Android-bearer run then sent that exact request with one disposable text source. The
named `ActOnSources` handler returned a nonempty JSON tree whose root was `NotebookLM Features`.
The adapter parsed the tree and persisted the same JSON through Android `CreateNote` within the
same lifecycle epoch; the response returned a canonical note ID. Deleting the exact disposable
notebook removed the note and source. Public Android assembly now uses this native generation and
note-write chain directly. Interactive generation remains the separate Android `CreateArtifact`
application path described above.

## Public assembly and local representation boundary

With explicit `backend="android"`, normal client assembly selects the complete eleven-namespace
Android adapter graph, including `AndroidArtifactsAPI`, `AndroidMindMapsAPI`, and
`AndroidAssetDownloadService`. The asset service participates in client lifecycle shutdown, and
generated protobuf modules remain lazy until an Android operation needs them. The selected mind-map
surface combines interactive Studio artifacts with the typed Android note-backed projection.

The local download surface accepts typed Android `Artifact` objects and exact protobuf artifacts
for infographic prefetch. Note-backed mind-map prefetch accepts typed `MindMap` objects; it does
not reinterpret Web positional rows. These paths close the public prefetch overloads without
widening the Android wire contract.

## Stable implementation contract

The inspected APK/AOT closure does not contain generated client bindings for `GenerateArtifact` or
`ExportToDrive`. The current website bundle nevertheless registers both operations on
`LabsTailwindOrchestrationService`, and its live call sites expose every request field used by the
product. Direct Android-bearer calls confirm that both fully qualified mobile paths route to
handlers:

```text
/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GenerateArtifact
/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/ExportToDrive
```

The conventional protobuf names below are web-derived because the APK has no descriptor entry.
Field tags, wire types, oneof behavior, and response semantics are independently pinned by the
current bundle call sites and route probes, so the implementation does not depend on those names:

```proto
message GenerateArtifactRequest {
  labs.language.tailwind.common.protos.RequestContext request_context = 1;
  string artifact_id = 2;
}

message GenerateArtifactResponse {
  Artifact artifact = 1;
}

message ExportToDriveRequest {
  labs.language.tailwind.common.protos.RequestContext request_context = 1;
  oneof input {
    string artifact_id = 2;
    string content = 3;
  }
  string title = 4;
  int32 destination = 5; // 1 = Google Docs, 2 = Google Sheets
}

message ExportToDriveResponse {
  string url = 1;
}
```

The retry call site constructs context `#1` and artifact ID `#2`, invokes web RPC `Rytqqe`, and
requires the nested `Artifact` at response `#1`. Export constructs context `#1`, sets exactly one
of artifact ID `#2` or literal content `#3`, sets title `#4` and destination `#5`, invokes web RPC
`Krh3pd`, and reads the returned URL from response `#1`.

## Live GenerateArtifact results

A safe invalid-ID request returned `NOT_FOUND`, proving the request reached the named handler. A
completed artifact in a copied scratch notebook returned `INVALID_ARGUMENT` and remained completed
on read-back. `GenerateArtifact` therefore enforces a failed-artifact precondition rather than
acting as an unrestricted regeneration endpoint.

Obtaining that precondition without modifying an original resource required additional bounded
checks:

- Account inventory found failed slide/video artifacts, but `CopyProject` deliberately omitted
  failed artifacts from the copy.
- `UpdateArtifact` rejected a `status` field-mask mutation with `INVALID_ARGUMENT`.
- Deleting the sole source immediately after a disposable video kickoff left the artifact
  processing for the bounded observation window rather than manufacturing a failure.
- The current bundle's `CancelGeneration` route returned `PERMISSION_DENIED` to the Android bearer.
- Replaying the exact generation options and source ordering of a failed slide against 48 copied
  scratch sources was accepted by `CreateArtifact`, but remained `PROCESSING` throughout the
  three-minute bounded poll. The copied notebook was then deleted and did not yield a safely
  retryable failed row.
- A final `CopyArtifactsAsync` attempt used the plausible context/source-project/destination-project
  wire prefix against an empty disposable destination. The route returned a nonempty asynchronous
  handle, but no artifact row appeared during the 72-second bounded read-back. The destination was
  deleted. This does not establish that helper's stripped request schema and is not an
  implementation basis.

These unsuccessful state-manufacturing attempts are not grounds to guess a success contract. The
implementation deserializes `GenerateArtifactResponse.artifact`, requires the returned ID to equal
the input ID, and projects its returned status. The mutation is one-attempt/non-replay-safe because
it has no client idempotency token and requeues generation in place. An accepted retry remains
unproven until a disposable failed-row fixture is available.

## ExportToDrive live validation and safety conclusion

The repository's scrubbed successful web cassette independently proves report-to-Docs with the
same logical fields and a one-element HTTPS Docs URL response. A newly authorized live probe then
copied a notebook containing a completed report and called the fully qualified `ExportToDrive`
path through the Android bearer with artifact ID `#2`, a disposable title `#4`, and destination
`#5 = 1`.

The call succeeded. The response contained exactly one length-delimited field `#1`; it decoded as
an HTTPS `docs.google.com/document/d/...` URL. The document ID remained only in memory. A Drive v3
exact-file read using the same short-lived bearer returned HTTP 200, matched that ID, and reported
MIME type `application/vnd.google-apps.document`. Deleting that exact file returned HTTP 204. This
proves report-to-Google-Docs through the Android bearer and closes the external cleanup requirement.
It does not prove Sheets, literal-content export, or other artifact/destination combinations.

The implementation marks the call non-replay-safe, accepts exactly one of artifact ID/content,
validates destination `1`/`2`, requires a nonempty HTTPS response URL, and surfaces transport loss
as an outcome-unknown mutation. A later Android Drive-file import probe independently exercised
the bearer-authenticated Drive v3 download and exact external-file cleanup described in the
resource-lifecycle record.

## Byte-transfer boundary

Audio and video each exposed progressive, HLS, DASH, and download endpoints. The authenticated
`lh3` resolution hop retained the bearer only on exact `lh3.googleusercontent.com`; it was stripped
before the terminal host. Ordinary anonymous GETs to progressive `.googlevideo.com` targets
succeeded: video was HTTP 200 `video/mp4` with ISO-BMFF `ftyp`, and audio was HTTP 200 `audio/wav`
with RIFF. The Android downloader admits that exact host/MIME/signature closure and prefers the
progressive single-file representation over the download endpoint. When a registry-derived
destination has an `.m4a` suffix but the response is verified WAV (`audio/wav` plus RIFF/WAVE), the
publisher changes the final suffix to `.wav`; batch downloads retain that returned path.

Download endpoints redirected to `drum.usercontent.google.com` and returned HTTP 403 without
credentials. No bearer was forwarded because the APK does not authorize that. Slide PDF/PPTX URLs
began on `contribution.usercontent.google.com`; an anonymous `alr=yes` request returned HTTP 302
`text/html`, while the same request with the Android multi-scope OAuth bearer returned HTTP 200
`application/octet-stream` with no redirect for both formats.

This matches the exact APK AOT control flow. `ArtifactDownloadManager.download` injects the session
`SSOHttpClient` into `artifact_download_utils.downloadWithAlr`, which appends `alr=yes`, issues an
ordinary `GET`, and switches to a raw client only for the configured storage-host patterns. Generic
multipart constants elsewhere in bundled `package:http` are unrelated; no Drive form POST
participates in this path.

The real Android public API then downloaded a 15,017,608-byte PDF with a valid `%PDF-` signature and
a 17,392,113-byte PPTX that was a valid OOXML ZIP containing `[Content_Types].xml` and `ppt/`
entries. The downloader now allows the observed generic MIME only for slide representations and
still requires the exact PDF or ZIP signature before atomic publication. Temporary files were
deleted after this read-only validation, which created no notebook or external Drive resource.

## Cleanup qualification

Every copied or created notebook used by these probes was deleted through `DeleteProjects`,
including the source-delete, status-mutation, unsupported-family, ready-artifact retry,
cancellation, data-table, media-option, and both mind-map experiments. Exact-prefix/title sweeps
found no remaining scratch notebook. The note-backed Android run deleted its exact notebook after
`ActOnSources` and `CreateNote` succeeded. The exported Google Doc and the later Drive-import file
were permanently deleted by exact ID through Drive v3 with HTTP 204 before their notebooks were
deleted. Credentials remained in memory or redacted throughout.
