# Android gRPC capability and signature evidence

**Status:** Stable consolidated evidence for APK-extracted signatures, web-derived signature
inference, and authenticated mobile-backend behavior

This document combines three complementary audits without collapsing their evidence boundaries:

- signed-APK analysis proves only what the shipped Android client compiled, including exact
  generated bindings and explicit method absence;
- authenticated web-bundle analysis supplies constructor identity and inferred protobuf names for
  methods absent from the APK;
- authenticated calls to `notebooklm-pa.googleapis.com` prove route availability, and only
  valid-resource calls with read-back prove operation semantics.

Routing, wire-shape acceptance, semantic success, inferred type names, and APK-extracted fully
qualified names are distinct claims throughout this report.

## Source-document provenance

These byte hashes identify the reports merged into this stable document. They preserve the original
audit boundary even after the dated files are removed.

| Original source | Evidence date | SHA-256 |
|---|---|---|
| `latest-apk-grpc-audit-2026-08-29.md` | 2026-08-29 | `418d3e67f419181774ee3c77c3f9b1e6d100a535fad8be371e76bc97fb8e8694` |
| `web-bundle-grpc-signature-inference-2026-08-29.md` | 2026-08-29 | `8518b301eeb3f6189fb16aba9266631a661a8abb81e5f6e06b056359e594b26f` |
| `web-parity-gap-live-validation-2026-08-27.md` | 2026-08-27 | `c0a3a16b2ff0eba18395e5a53ae2ebddb3b299d8b2cae0d6d868a3e294b08251` |

## Signed APK static evidence

**Audit date:** 2026-08-29

**Package:** `com.google.android.apps.labs.language.tailwind`

**Version:** `1.55.10.971450265` (`versionCode=153888`)
**Target:** Android arm64; Dart `3.14.0-166.0.dev`

This audit tests whether a newer official-signed client can close the seven implemented gRPC
signature exceptions that are absent from the older `1.46.7.940945420` AOT image. It does not use
successful wire decoding as proof of a protobuf fully qualified name (FQN).

### Acquisition and signing identity

APKMirror's download gate returned HTTP 403, so `apkeep 1.0.0` acquired the XAPK through its
`apk-pure` source. The [Google Play listing](https://play.google.com/store/apps/details?id=com.google.android.apps.labs.language.tailwind)
and [APKMirror's arm64 release page](https://www.apkmirror.com/apk/google-inc/google-notebooklm/gemini-notebook-1-54-8-967070991-release/gemini-notebook-1-54-8-967070991-android-apk-download/)
provide independent package/publisher and signing-certificate metadata.

Android `apksigner` verifies every split under APK Signature Scheme v3 and verifies the source
stamp. The signer is `CN=Android, OU=Android, O=Google Inc., L=Mountain View, ST=California, C=US`.

| Identity | SHA-256 |
|---|---|
| APK signer certificate | `ba49176908275f83be9ae1034968f0b18e65177a64e5a40b3a621f148dfb6fa2` |
| source-stamp signer certificate | `3257d599a49d2c961a471ca9843f59d341a405884583fc087df4237b733bbd6d` |
| XAPK | `67fafb471ffa50379b36e8a33879292a7ff52eb04f4460d33f54db28dc51ad78` |
| base APK | `6552f1192135ccd83ebd0feb62534f842d6697c99be8855b847ca5ead544c81a` |
| arm64 split APK | `8206eb94fe01620f4388a141cdff3a07690bf06029e1277a763a1ca7ece80698` |
| Dart AOT image | `77bff7507e393c092b78ff1756bb3d726881050b22728dcc8c46cf0fecd7cda7` |
| `libflutter.so` | `ee1d2af8af0f80dea3f5c605d6537cfd2d80a28912eeb39a6ab918725d0f671c` |
| acquisition metadata response | `70cecd9b821842883e26fdf76bf1bd0080742147d1be5e4cd03027b60c79a525` |

The source stamp reports `2026-08-28T17:51:46Z`. The APK certificate hash matches the certificate
published independently on APKMirror.

## Blutter result

Blutter completed in an isolated temporary copy. Two code-analysis warnings were nonfatal; the
object pool, IDA naming script, and reconstructed generated protobuf clients were produced.

| Evidence | SHA-256 |
|---|---|
| `blutter-out/pp.txt` | `c2b64fd7d08a64f833b343f54bc697520096dfaef10740ebcbcd66a5c8e24b9a` |
| `blutter-out/ida_script/addNames.py` | `b75adf9f8bb92085c853dd30d231d533aec987024cf0e442f7cafc03dab24518` |
| [53-path inventory](../../tests/fixtures/android/latest_apk_grpc_paths.txt) | `b5df4996f271e71ccc14e0ae0f8eaa13e1e337b4bc726b54a487a0c4f6d31697` |
| [52 exact signatures](../../tests/fixtures/android/latest_apk_grpc_signatures.csv) | `6381163929c18d51eb654bc677846061ea65e9d501b9beb9db3952b749b32b7c` |

`scripts/extract_blutter_grpc_signatures.py` recovered 52 exact generated-client bindings. The
binary contains 47 orchestration full paths: 46 have adjacent exact request/response generic
bindings, while `UpsertArtifactUserState` remains the sole present orchestration path without one.

Compared with the older `1.46.7.940945420` binary, this build adds exact bindings for
`CancelGeneration`, `ListArtifactScheduledNotificationConfigs`,
`UpdateArtifactScheduledNotificationConfig`, `DiscoveryService/BatchSearchNotebooks`, and
`DiscoveryService/SearchNotebooks`. Its two-method `DiscoveryService` replaces the old
`LabsTailwindDiscoveryService/PrototypeNotebookSearch` call site. The committed inventories are
version-scoped so this delta cannot silently change the older capture narrative.

### Seven-method absence result

Each target was searched as a method name and full-path suffix across the decompressed XAPK, AOT
image, native libraries, Dex/resources, `pp.txt`, `addNames.py`, and reconstructed Dart output.

| RPC | AOT image | `pp.txt` | `addNames.py` | Dex/resources/native tree |
|---|---:|---:|---:|---:|
| `CopyProject` | 0 | 0 | 0 | 0 |
| `MutateSource` | 0 | 0 | 0 | 0 |
| `GenerateReportSuggestions` | 0 | 0 | 0 | 0 |
| `CreateLabel` | 0 | 0 | 0 | 0 |
| `MutateLabel` | 0 | 0 | 0 | 0 |
| `DeleteLabels` | 0 | 0 | 0 | 0 |
| `CancelDiscoverSourcesJob` | 0 | 0 | 0 | 0 |

The newer signed client therefore cannot supply exact request/response FQNs for these methods.
They were tree-shaken or never compiled into the application. This client-absence result does not
limit mobile-backend capability: a later authenticated web-bundle audit and valid-resource mobile
calls promoted all seven into the generated service. Six conventional type-name guesses remain
explicit in [`grpc-service-signature-inferences.json`](grpc-service-signature-inferences.json);
they are web-derived inferences, not claims of APK extraction.

### Reproduction

The complete audit workspace was retained in an isolated temporary directory on the audit host.
That workspace was ephemeral; the hashes above are the durable identity boundary. With `apkeep`,
`apksigner`, and Blutter available, the equivalent command sequence is:

```bash
./apkeep \
  -a com.google.android.apps.labs.language.tailwind \
  -d apk-pure \
  ./audit-workspace/downloads

apksigner verify --verbose --print-certs \
  ./audit-workspace/xapk/com.google.android.apps.labs.language.tailwind.apk

uv run python ./blutter/blutter.py \
  ./audit-workspace/blutter-input \
  ./audit-workspace/blutter-out

uv run python scripts/extract_blutter_grpc_signatures.py \
  ./audit-workspace/blutter-out/pp.txt \
  ./audit-workspace/blutter-out/ida_script/addNames.py
```

## Web-bundle-derived signature evidence

**Audit date:** 2026-08-29
**Authenticated bundle SHA-256:** `8cc2569196b28083ba58a33319df79af97ec1832f442c4a182289894edf5eaef`

The authenticated NotebookLM web bundle confirms all 48 project RPC IDs used by this repository,
including methods absent from both audited Android APKs. The web registry constructor order is
response first, request second:

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
| `yR9Yof` | `CheckSourceFreshness(CheckSourceFreshnessRequest) returns (CheckSourceFreshnessResponse)` | request carries `SourceId #2`, context `#3`; response wraps freshness at `#1`, whose optional bool is `#2` and ID is `#3`; valid-resource Android read succeeded | type names conventional; admitted partial wire shape pinned |
| `FLmJqe` | `RefreshSource(RefreshSourceRequest) returns (RefreshSourceResponse)` | current constructor carries `SourceId #2`, context `#3`; response wraps `Source #1`; a stale native Google Doc refreshed successfully through the Android bearer | type names conventional; wire shape and valid-resource semantics live pinned |
| `ciyUvf` | `GenerateReportSuggestions(GenerateReportSuggestionsRequest) returns (GenerateReportSuggestionsResponse)` | request context/project/repeated source IDs at `#1/#2/#3`; response repeated suggestions `#1`, with admitted title/description/repeated source IDs/prompt/audience at `#1/#2/#4/#5/#6`; an accessed nested field `#3` remains unknown | type names conventional; admitted partial wire shape pinned |
| `agX4Bc` | `CreateLabel(CreateLabelRequest) returns (CreateLabelResponse)` | request context `#1`; oneof auto-create `#5` versus manual-create `#6`; optional regenerate-all bool `#1`; response repeated labels `#2` and collections `#3`; live collection rows pin name/notebook IDs/ID/emoji at `#1/#2/#3/#4`; false/true/default automatic-label modes and manual CRUD/read-back succeeded | type names conventional; admitted response wire shape pinned for both resource kinds |
| `le8sX` | `MutateLabel(MutateLabelRequest) returns (MutateLabelResponse)` | request context `#1`; dedicated response ctor distinct from protobuf `Empty`; no response fields are retained/used; property/membership mobile read-back succeeded | type names conventional; no-field partial parser inferred |
| `GyzE7e` | `DeleteLabels(DeleteLabelsRequest) returns (DeleteLabelsResponse)` | request context `#1`; dedicated response ctor distinct from protobuf `Empty`; no response fields are retained/used; mobile delete/read-back succeeded | type names conventional; no-field partial parser inferred |
| `Zbrupe` | `CancelDiscoverSourcesJob(CancelDiscoverSourcesJobRequest) returns (google.protobuf.Empty)` | request context `#1`; response ctor is identical to `DeleteProjects`, `DeleteSources`, `DeleteArtifact`, and `DeleteChatTurns`, whose Android bindings independently prove `google.protobuf.Empty`; mobile cancel lifecycle succeeded | request type exact from pinned package source and augmented with current context field; response exact by ctor bridge |
| `Rytqqe` | `GenerateArtifact(GenerateArtifactRequest) returns (GenerateArtifactResponse)` | context/artifact id at `#1/#2`; response wraps `Artifact #1`; a valid READY artifact reached retryability validation and was rejected as non-retryable | type names conventional; request/response wire and handler semantics pinned |
| `Krh3pd` | `ExportToDrive(ExportToDriveRequest) returns (ExportToDriveResponse)` | context, artifact/content oneof, title, destination at `#1/#2-or-3/#4/#5`; valid report-to-Docs returned URL `#1`, and Drive read-back/delete succeeded | type names conventional; report-to-Docs live-pinned, other target/destination combinations web-derived |

The ten conventional rows are deliberately recorded in
`grpc-service-signature-inferences.json`. Protobuf type names are not serialized on the wire, so
mobile calls and handler validation prove message layout but cannot alone prove those names. They
are generated to provide the complete callable service requested by the project, without
relabeling them as APK-extracted facts.

Constructor identity is also preserved inside the organization request closure: manual-create
properties, mutate properties, and the four add/remove source/notebook payloads use distinct
generated message types even though their currently observed bytes are structurally equivalent.
The empty partial parsers for mutate/delete responses mean only that the bundle retained no fields;
they do not claim the remote messages are literally fieldless.

The APK absence result remains unchanged: the official clients simply do not compile these seven
call sites. APK inventory is a client-feature inventory, not a mobile-backend capability list.

## Authenticated Android-backend parity validation

**Live validation:** 2026-08-27
**Status:** Mobile routing checked for every `notebooklm-py` method absent from the inspected APK

This run answers a narrower question than the APK inventory: when the Android binary does not
compile a method, does `notebooklm-pa.googleapis.com` still route that method for an Android OAuth
bearer? The answer is yes for all 15 current gaps, but routing and usable semantics are not the
same claim.

No credential, notebook/source/artifact ID, title, source URL, or source content was printed. A
source- and artifact-rich owned notebook was copied first. All valid mutations and deletes below
targeted only that disposable copy, and the copy was deleted at the end.

## Test target and copy fidelity

The new web client operation was used from the isolated branch
`feat/mobile-copy-endpoint-audit`:

```python
copied = await client.notebooks.copy(source_notebook_id, disposable_title)
```

It sends `te3DCe → CopyProject` with:

```text
#1 RequestContext
#2 source project UUID
#3 destination title
```

The selected original had 50 sources and 5 Studio artifacts:

```text
sources: 44 web pages, 4 PDFs, 1 CSV, 1 Markdown
artifacts: 1 audio, 1 report, 1 data table, 1 infographic, 1 slide deck
```

The copy returned all 50 sources and all 5 artifacts on its first read-back. The intersection
between original and copied source IDs was empty, and the intersection between original and copied
artifact IDs was empty. This upgrades the earlier one-source probe: `CopyProject` copies both
sources and Studio artifacts, allocating new child identities.

### The 15 APK-absent web methods

| family | methods absent from APK | mobile bearer/gRPC result |
|---|---|---|
| labels/collections | `CreateLabel`, `MutateLabel`, `DeleteLabels` | valid-resource success; full CRUD and membership read-back |
| async Research | `DiscoverSourcesManifold`, `DiscoverSourcesAsync`, `ListDiscoverSourcesJob`, `CancelDiscoverSourcesJob` | valid lifecycle success |
| notebook copy | `CopyProject` | valid 50-source/5-artifact copy success |
| source maintenance | `MutateSource`, `CheckSourceFreshness` | valid copied-source success |
| report suggestions | `GenerateReportSuggestions` | valid copied-notebook success; four rows |
| source refresh | `RefreshSource` | route exists, but valid copied URL source returned `INVALID_ARGUMENT` |
| side-effect routes | `GenerateArtifact`, `ExportToDrive`, `ShareAudio` | `NOT_FOUND` for nonexistent UUIDs; route proven without creating state |

Every one returned something other than `UNIMPLEMENTED`, so all 15 method paths are present on the
mobile host. Only the rows marked valid-resource success establish the operation's semantics.

## Newly recovered successful request shapes

The request context was optional for the successful calls below.

## MutateSource

```text
request:
  #2 SourceId { #1 source UUID }
  #3 repeated SourceMutation {
       #1 ChangeTitle { #1 new title }
     }
  #4 RequestContext (optional in this replay)
```

The copied source's title changed and the web read path returned the new title.

## CheckSourceFreshness

```text
request:
  #2 SourceId { #1 source UUID }
  #3 RequestContext (optional in this replay)
```

The nested field-2 ID is load-bearing. A raw string at field 2, or the same nested/string value at
field 1, returned `INVALID_ARGUMENT`. The valid nested request returned gRPC success; its empty
response is the same healthy shape the web decoder interprets as fresh.

### GenerateReportSuggestions

```text
request:
  #1 RequestContext (optional in this replay)
  #2 project UUID
  #3 repeated SourceId (optional filter; omitted here)

response:
  #1 repeated suggestion
```

The copied notebook returned four suggestion rows.

### Historical RefreshSource result: route present, operation rejected

The current web bundle builds the same semantic request as freshness checking:

```text
#2 SourceId { #1 source UUID }
#3 RequestContext
```

The direct mobile test tried all of these context variants with the correct nested source ID:

1. minimal web context (`clientType = 2`);
2. minimal Android context (`clientType = 3`);
3. an encoded empty context message;
4. the full Android version/provenance context used by the upload reproducer.

All four returned `INVALID_ARGUMENT`. Field-2 raw string and field-1 nested/raw alternatives also
returned `INVALID_ARGUMENT`. This is not an unknown route: `UNIMPLEMENTED` was never returned.

As a control, web `FLmJqe` refreshed three copied URL sources successfully, including sources whose
freshness check was already true. The evidence therefore supports this precise statement:

> `RefreshSource` is routed by the mobile gRPC host, but a request that is valid through the web
> transport was rejected through the mobile bearer endpoint in this account/build cohort.

It does not support claiming full mobile refresh parity yet. A future probe should use a copied
Google Drive source and an actual Android capture if one becomes UI-reachable.

### Safe route-only probes for high-side-effect methods

These calls used canonical but nonexistent UUIDs. A `NOT_FOUND` response proves the request reached
the named handler while ensuring no valid artifact was generated, exported, or shared.

```text
GenerateArtifact:
  #1 RequestContext
  #2 nonexistent artifact UUID
  result: NOT_FOUND

ExportToDrive:
  #1 RequestContext
  #2 nonexistent artifact UUID
  #4 disposable title
  #5 destination = 1 (Docs)
  result: NOT_FOUND; no Drive file created

labs.language.tailwind.sharing.LabsTailwindSharingService/ShareAudio:
  #1 share option
  #2 copied project UUID
  #3 nonexistent artifact UUID
  result: NOT_FOUND; no share state changed
```

These are route checks, not semantic success tests. A valid-ID export would create a Drive file and
a valid-ID share could change public state, so neither was justified merely to prove dispatch.

## APK-present destructive validation on the copy

The same run also filled several "compiled but not UI-exercised" gaps:

| operation | result |
|---|---|
| `CreateNote` | created a disposable note; ID and row read back |
| `MutateNote` | content and title changed; both read back |
| `DeleteNotes` | gRPC success; immediate lookup briefly saw the row, next list excluded it |
| `UpdateArtifact` | copied report title changed and read back |
| `DeleteArtifact` | copied report disappeared; artifact count changed 5 → 4 |
| `DeleteSources` | renamed copied URL source disappeared; source count changed 50 → 49 |

`RemoveRecentlyViewedProject` was also attempted against the owned copy with a valid Android
context. It returned `INTERNAL`, so it is not marked live-successful in the endpoint matrix. A
current-context follow-up used the APK-exact request (`project_id #1`, `RequestContext #2`) against
a fresh uniquely named notebook. It again returned gRPC 13 `INTERNAL`; the notebook remained in
`ListRecentlyViewedProjects` and remained readable. This closes the request shape while confirming
a server-handler gap with no state change. Both runs deleted their exact scratch notebook and
verified absence.

## Corrective Android-backend qualification — 2026-08-29

Later bounded probes corrected the historical RefreshSource mismatch and admitted native settings
and note-backed mind-map paths. Each claim below distinguishes APK-exact identity from
current-bundle field recovery and from live semantic proof.

### RefreshSource valid-resource success

The 2026-08-27 URL-source failures above remain accurate for that resource cohort. The current
authenticated bundle independently closes the request as `SourceId #2` plus `RequestContext #3`
and the response as `Source #1`. A later run created a nonempty native Google Doc through Drive
conversion, added it to a disposable notebook, and first observed `CheckSourceFreshness = false`.
The exact `RefreshSource` request then succeeded through the Android bearer. The Drive document was
deleted by exact ID with HTTP 204, and the exact notebook cleanup succeeded. Refresh is therefore a
native Android operation; the earlier `INVALID_ARGUMENT` result is no longer a public parity gap.

### Native note-backed mind-map generation

The official APK supplies the exact `ActOnSourcesRequest`/`ActOnSourcesResponse` FQNs and request
fields `sources #1`, options `#2`, free-form action `#3`, source options `#7`, request context `#8`,
chat-session ID `#10`, and origin `#11`. The current authenticated Web constructor closes action
field `#6` as action string `#1`, repeated key/value context rows `#2`, and language `#3`.

A bounded Android-bearer run sent action `interactive_mindmap` over one disposable text source.
`ActOnSources` returned a nonempty JSON tree rooted at `NotebookLM Features`; Android `CreateNote`
then persisted the exact JSON and returned a canonical note ID within the same operation epoch. The
exact disposable notebook was deleted. This replaces the former Web generation collaborator.

### Native citation-rich saved chat notes

The older APK note descriptor omits rich CreateNote fields, but the current authenticated Web
protobuf constructor closes the active server contract: repeated `Citation source_passages #4`,
`TailwindDoc #6`, and `RequestContext #7` alongside the established project/content/metadata/name
fields `#1/#2/#3/#5`. The same current response constructor exposes `ProjectNote` citations `#4`
and rich document `#6`. `Citation` carries repeated ranges `#4`, fragment `#5`, source attribution
`#6`, and object ID `#7`.

A live Android-bearer `CreateNote` saved a marked answer using that exact current-server overlay.
`GetNotes` returned `ProjectNote` fields `#1` through `#6`; its `TailwindDoc` retained body `#1`,
objects `#4`, the source identity/range/fragment/object links, and the inline marker anchors. The
plain marker-bearing content also persisted. The server normalized the top-level source-passage row
to an empty placeholder, while the full citation remained intact in the document object. The exact
scratch notebook was deleted and a title-prefix sweep was clean. `chat.save_answer_as_note` is
therefore native without citation loss.

### Authenticated slide representation transfer

Exact APK AOT call-flow evidence identifies slide download as an SSO HTTP client GET with
`alr=yes`; the earlier multipart form-token association was false. Authentication remains only for
configured Google storage hosts and is stripped before untrusted redirect targets. A live bearer
GET to a `contribution.usercontent.google.com` representation returned HTTP 200 octet-stream, while
the anonymous control returned a 302 to sign-in HTML. The public Android downloader then saved both
formats: a 15,017,608-byte PDF beginning `%PDF-` and a 17,392,113-byte PPTX validated as an OOXML
ZIP. Slide-deck transfer is therefore native and no longer blocked by an unrecovered token flow.

### Native account settings with restoration

The APK-exact `MutateAccount` method accepts repeated `AccountMutation #1` plus
`RequestContext #2`; the mutation oneof selects `change_property #2`, whose nested
`new_user_info #1` carries `UserInfo.output_language #5` and `OutputLanguage.language_code #1`.
Current authenticated Web constructors and accessors establish that nested closure.
`GetOrCreateAccountRequest` carries `RequestContext #1`; its response wraps `Account #1`.

The same account response carries `TierLimits #2`: current Web mapping and live decoding establish
optional integer fields `#1` through `#5`, including project/source/word limits at `#2/#3/#4`.
The semantic name of field `#1` remains conventional/opaque. A restore-safe live run captured the
prior nonempty output language, mutated to a temporary value, read it back, restored the prior
language in `finally`, and verified restoration with a fresh native read. All four public settings
methods are consequently native.

### Sharing collaborator contract boundary

Current authenticated Web builders close `ShareProject` collaborator writes: user target email
`#1` (alternate ID `#4`), permission `#3`, repeated user permissions under project field `#2`,
welcome-message fields `#1/#2` under project field `#4`, top-level notification `#2`, and request
context `#4`. Current accessors close `GetProjectDetailsResponse.shared_users #1`, including email
`#1`, permission `#2`, profile `#4`, and profile display/avatar `#1/#2`.

These exact bytes and stateful write/read-back behavior are pinned by focused contract tests. No
live invitation or removal was attempted without a controlled secondary identity, so this is not
claimed as live collaborator-side-effect proof. At this 2026-08-29 checkpoint, public link and
collaborator operations were native while the independently rejected view-level probe remained a
Web compatibility seam. Follow-up work showed that probe had targeted the wrong service and moved
view-level mutation to native `MutateProject`; see
[`web-compat-seam-closure.md`](web-compat-seam-closure.md#sharingset_view_level--wrong-service).

### What this changes

- APK method strings are a **client inventory**, not a backend capability list.
- The current mobile host routes every method in the 48-method web library surface.
- Eleven of the 15 APK gaps have valid-resource semantic proof.
- Three more have safe route proof only.
- The later stale-Google-Doc probe closes `RefreshSource` with valid-resource Android success.
- Exact `RemoveRecentlyViewedProject` reaches a failing server handler for an
  owned project and changes no state; later two-account evidence established
  native success for shared projects and the public adapter's owned-project
  no-op rule.
- Native account settings and note-backed mind-map generation have live mutation/read-back proof;
  collaborator sharing is bundle/byte/test qualified without a live invitation.

The complete 48-row cross-reference is maintained in [endpoints.md](endpoints.md).
