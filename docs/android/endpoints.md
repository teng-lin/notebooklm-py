# NotebookLM Android API — endpoints and message shapes

**Status:** Recovered from capture, schema-level (field numbers + wire types), not
field-named by Google

**Last verified:** 2026-09-01 (`1.46.7` capture snapshot plus `1.55.10` binary delta; chat session status/cancel re-probed live)

**Scope:** the original `1.46.7.940945420` **49-method surface** (4 gRPC services) is enumerated
and cross-referenced to the 48-method Web registry used for that audit. The newer Google-signed
`1.55.10.971450265` inventory contains 53 methods and is recorded as a version-scoped delta below.
The original traffic capture exercised 21 methods and decoded their wire shapes here; later direct
bearer/gRPC probes also exercised APK-unwired methods and destructive APK-present methods on
disposable copies. The
**complete protobuf schema** — 323 messages / 868 fields with real field names, tags, types, and
cardinality — was recovered by decompiling the Flutter binary with a Dart-ported blutter (3.13 for the
`1.46.7` snapshot, 3.14 for the current `1.55.10` regeneration), and
is checked in at **[android/schema.proto](schema.proto)**. The inline shapes below keep their
wire-capture form (field `#N` + type); the `.proto` file is authoritative for names. Read paths
were driven on real notebooks; mutations were confined to throwaway notebooks or copies and
cleaned up afterward.

This document is the schema-recovery follow-up to
[docs/android/capture.md](capture.md), which explains how the `.pb`
bodies were captured. Regenerate the raw shapes with:

```bash
python scripts/decode_mobile_grpc.py /tmp/notebooklm-mobile-grpc <MethodSubstring>
```

The decoder redacts string/bytes **values** (prints only lengths); this doc inherits
that redaction. Do not paste real capture output into the repo — protobuf bodies
carry notebook IDs, source text, and chat history.

## Service and transport

| Property | Value |
|---|---|
| Host | `notebooklm-pa.googleapis.com:443` |
| Service | `google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService` |
| Path | `/<service>/<Method>` |
| Transport | HTTP/2 `POST`, `content-type: application/grpc` |
| Framing | one length-prefixed protobuf message per body (5-byte gRPC envelope) |
| Auth | OAuth bearer header, added by the app (not captured) |
| Success | HTTP 200 + trailer `grpc-status: 0` |

Most methods are unary (single request message, single response message);
`GenerateFreeFormStreamed` and `StreamLiveSession` are server-streaming.

## Complete service surface (`1.46.7` binary snapshot)

The original 21 captured RPCs whose shapes are documented below are the ones the mobile UI called
in that capture. Later sections add direct parity probes; they are labelled separately and must not
be mistaken for APK call-site evidence. The **full** client surface is larger: extracting
method-path strings from the Flutter AOT
library (`lib/arm64-v8a/libNotebookLM_prod_android_library_flutter_artifacts.so` in
`split_config.arm64_v8a.apk`) enumerates **49 methods across 4 gRPC services** compiled into
that app version — many exist but are not wired to any mobile screen.

```bash
strings -a <libNotebookLM_...flutter_artifacts.so> \
  | grep -oE '/[a-z][a-z0-9_.]+\.[A-Z][A-Za-z0-9]+/[A-Z][A-Za-z0-9]+' | sort -u
```

| Service | Methods |
|---|---|
| `LabsTailwindOrchestrationService` (44) | ActOnSources, AddSources, AddTentativeSources, CreateArtifact, CreateNote, CreateProject, DeleteArtifact, DeleteChatTurns, DeleteNotes, DeleteProjects, DeleteSources, DeriveArtifact, DiscoverSources, FinishDiscoverSourcesRun, GenerateAccessToken, GenerateDocumentGuides, GenerateFreeFormStreamed, GenerateNotebookGuide, GeneratePromptSuggestions, GetArtifact, GetArtifactCustomizationChoices, GetArtifactUserState, GetDriveSourceStatus, GetIceConfig, GetLabels, GetNotes, GetOrCreateAccount, GetProject, ListArtifacts, ListChatSessions, ListChatTurns, ListExpertIntelligenceContent, ListRecentlyViewedProjects, LoadSource, LogInteractionEvent, MutateAccount, MutateNote, MutateProject, RemoveRecentlyViewedProject, SendSdpOffer, SubmitFeedback, SuggestArtifacts, UpdateArtifact, UpsertArtifactUserState |
| `LabsTailwindSharingService` (3) | CreateAccessRequest, GetProjectDetails, ShareProject |
| `LabsTailwindDiscoveryService` (1) | PrototypeNotebookSearch |
| `LiveSessionService` (1) | StreamLiveSession |

### Newer `1.55.10` binary delta

The 2026-08-29 Google-signed build audit enumerates **53 methods across 4 services**: 47
`LabsTailwindOrchestrationService`, 3 `LabsTailwindSharingService`, 2
`google.internal.labs.tailwind.api.v1.DiscoveryService`, and 1 `LiveSessionService`. Blutter proves
52 exact generic request/response bindings; only the present `UpsertArtifactUserState` path lacks
an adjacent binding.

Relative to the table above, the newer binary adds `CancelGeneration`,
`ListArtifactScheduledNotificationConfigs`, `UpdateArtifactScheduledNotificationConfig`,
`DiscoveryService/BatchSearchNotebooks`, and `DiscoveryService/SearchNotebooks`. It drops the old
`LabsTailwindDiscoveryService/PrototypeNotebookSearch` call site. The complete current path and
signature fixtures are pinned in the
[latest APK audit](grpc-capability-and-signature-evidence.md#blutter-result); this historical capture
section is intentionally not rewritten to pretend those methods existed in `1.46.7`.

Notable methods present in the `1.46.7` binary but **not exercised by the mobile UI**: `CreateNote` /
`MutateNote` / `DeleteNotes` (note CRUD — UI only reads via `GetNotes`), `ActOnSources` (mind
maps on web), `DeriveArtifact` / `UpdateArtifact` / `SuggestArtifacts`, `GeneratePromptSuggestions`,
`MutateAccount`, `SubmitFeedback`, `GetIceConfig` / `SendSdpOffer` / `StreamLiveSession` (WebRTC
interactive-audio "Live"), `GetDriveSourceStatus`, `ListExpertIntelligenceContent`, and
`PrototypeNotebookSearch` (experimental search).

## Android ⇄ web cross-reference

**Same backend, two front doors.** Both clients drive the *same* Tailwind services; only the
transport differs:

| | Android | Web (`notebooklm-py` / browser) |
|---|---|---|
| Transport | native **gRPC**, HTTP/2 | **batchexecute**, HTTP POST |
| Endpoint | `notebooklm-pa.googleapis.com/<Service>/<Method>` | `/_/LabsTailwindUi/data/batchexecute` |
| Method id | plaintext `Method` name | 6-char obfuscated `rpcid` (e.g. `CCqFvf`) |
| Payload | protobuf, numbered fields | JSON arrays, positional fields |
| Auth | OAuth bearer | cookies / SAPISID |
| Chat | `GenerateFreeFormStreamed` (gRPC stream) | separate `QUERY_URL` endpoint |

The version-scoped `1.46.7` and Web inventories used by this cross-reference are exact, not
estimates:

- Android APK `1.46.7`: **49** compiled methods. The current audited `1.55.10` build has **53**;
  see the version-scoped delta above.
- `notebooklm-py` `RPCMethod`: **48** methods. A freshly downloaded web bundle confirmed all 48,
  including `te3DCe → CopyProject`; none were absent or merely text-present/unparsed. The same
  bundle contained another 123 parsed registrations not yet modelled by the library.
- `1.46.7` intersection by exact server method name: **33**.
- `1.46.7` APK-only relative to the 48-method batchexecute registry: **16**.
- Implemented by the web library but absent from the `1.46.7` APK: **15**.

For the separately audited `1.55.10` build, the intersection remains **33**, APK-only grows to
**20**, and Web-only remains **15**. The five-added/one-removed binary delta is listed above.

"Absent from the APK" means only that this app build does not compile a caller. It does **not**
mean the mobile bearer/gRPC backend lacks a route. The original direct probe found all 15 routes:
eleven were semantically successful with valid disposable resources, three high-side-effect methods
were route-probed with nonexistent UUIDs (`NOT_FOUND`), and `RefreshSource` rejected a valid copied
URL source with `INVALID_ARGUMENT`. A 2026-08-29 corrective probe then refreshed a stale native
Google Doc successfully through the Android bearer; the historical URL-source failure remains a
resource-cohort result, not a current parity gap.

Status vocabulary below:

- **captured** — observed from app traffic;
- **live** — a direct mobile bearer/gRPC call was verified by read-back;
- **route only** — a nonexistent target returned `NOT_FOUND`, proving dispatch without causing the
  Drive/share/generation side effect;
- **compiled** — present in the APK/schema but not independently exercised here.

### Complete web-library → APK/backend matrix (49/49)

| Server method | web `rpcid` (constant) | APK | mobile backend evidence |
|---|---|---:|---|
| ListRecentlyViewedProjects | `wXbhsf` (LIST_NOTEBOOKS) | ✅ | compiled; web live |
| CreateProject | `CCqFvf` (CREATE_NOTEBOOK) | ✅ | captured |
| **CopyProject** | `te3DCe` (COPY_NOTEBOOK) | ❌ | **live**; 50 sources + 5 artifacts copied with distinct child IDs |
| GetProject | `rLM1Ne` (GET_NOTEBOOK) | ✅ | captured |
| MutateProject | `s0tc2d` (RENAME_NOTEBOOK) | ✅ | captured; generic notebook mutator |
| DeleteProjects | `WWINqb` (DELETE_NOTEBOOK) | ✅ | captured |
| RemoveRecentlyViewedProject | `fejl7e` (REMOVE_RECENTLY_VIEWED) | ✅ | route returned `INTERNAL` for an owned disposable copy |
| AddSources | `izAoDd` (ADD_SOURCE) | ✅ | captured |
| AddTentativeSources | `o4cbdc` (ADD_SOURCE_FILE) | ✅ | captured; file/tentative registration path |
| DeleteSources | `tGMBJ` (DELETE_SOURCE) | ✅ | captured + live delete/read-back on copied source |
| LoadSource | `hizoJc` (GET_SOURCE) | ✅ | captured |
| **RetrieveRelevantChunks** | `ASU5Oe` (RETRIEVE_RELEVANT_CHUNKS) | ❌ | **live** unfiltered and source-filtered native calls; [wire evidence](source-search-evidence.md) |
| **MutateSource** | `b7Wfje` (UPDATE_SOURCE) | ❌ | **live** title mutation + read-back |
| **RefreshSource** | `FLmJqe` (REFRESH_SOURCE) | ❌ | **live** on a stale native Google Doc; an earlier copied-URL probe returned `INVALID_ARGUMENT` for four context variants |
| **CheckSourceFreshness** | `yR9Yof` (CHECK_SOURCE_FRESHNESS) | ❌ | **live** on copied URL source |
| **CreateLabel** | `agX4Bc` (CREATE_LABEL) | ❌ | **live** for source labels and notebook collections |
| GetLabels | `I3xc3c` (LIST_LABELS) | ✅ | APK + live for both resource kinds |
| **MutateLabel** | `le8sX` (UPDATE_LABEL) | ❌ | **live** properties and memberships |
| **DeleteLabels** | `GyzE7e` (DELETE_LABEL) | ❌ | **live** for both resource kinds |
| GenerateNotebookGuide | `VfAZjd` (SUMMARIZE) | ✅ | captured |
| GenerateDocumentGuides | `tr032e` (GET_SOURCE_GUIDE) | ✅ | captured |
| **GenerateReportSuggestions** | `ciyUvf` (GET_SUGGESTED_REPORTS) | ❌ | **live**; four suggestion rows returned |
| CreateArtifact | `R7cb6c` (CREATE_ARTIFACT) | ✅ | captured |
| ListArtifacts | `gArtLc` (LIST_ARTIFACTS) | ✅ | captured |
| DeleteArtifact | `V5N4be` (DELETE_ARTIFACT) | ✅ | **live** delete/read-back on copied report |
| UpdateArtifact | `rc3d8d` (RENAME_ARTIFACT) | ✅ | **live** title mutation + read-back |
| **ExportToDrive** | `Krh3pd` (EXPORT_ARTIFACT) | ❌ | **live backend overlay**: report-to-Docs succeeded; Drive read-back and exact deletion succeeded |
| **ShareAudio** | `RGP97b` (SHARE_ARTIFACT) | ❌ | **route only** on sharing service (`NOT_FOUND`; no share state changed) |
| GetArtifact | `v9rmvd` (GET_INTERACTIVE_HTML) | ✅ | captured; generic artifact getter |
| DeriveArtifact | `KmcKPe` (REVISE_SLIDE) | ✅ | compiled; generic derive operation |
| **GenerateArtifact** | `Rytqqe` (RETRY_ARTIFACT) | ❌ | **live backend overlay**: wire pinned; valid READY artifact rejected as non-retryable; accepted failed-row replay not yet captured |
| **DiscoverSources** | `Es3dTe` (DISCOVER_SOURCES) | ✅ | **live** synchronous discovery (`research.discover()`; ten rows, overview, job id) |
| **DiscoverSourcesManifold** | `Ljjv0c` (START_FAST_RESEARCH) | ❌ | **live** fast research start |
| **DiscoverSourcesAsync** | `QA9ei` (START_DEEP_RESEARCH) | ❌ | **live** deep research start |
| **ListDiscoverSourcesJob** | `e3bVqc` (POLL_RESEARCH) | ❌ | **live** poll |
| FinishDiscoverSourcesRun | `LBwxtb` (IMPORT_RESEARCH) | ✅ | **live** import |
| **CancelDiscoverSourcesJob** | `Zbrupe` (CANCEL_RESEARCH) | ❌ | **live** cancel |
| ActOnSources | `yyryJe` (GENERATE_MIND_MAP) | ✅ | compiled; generic source action |
| CreateNote | `CYK0Xb` (CREATE_NOTE) | ✅ | **live** create/read-back on copied notebook |
| GetNotes | `cFji9` (GET_NOTES_AND_MIND_MAPS) | ✅ | captured |
| MutateNote | `cYAfTb` (UPDATE_NOTE) | ✅ | **live** content/title mutation + read-back |
| DeleteNotes | `AH0mwd` (DELETE_NOTE) | ✅ | **live**; deletion became visible on the next read |
| ListChatSessions | `hPTbtc` (GET_LAST_CONVERSATION_ID) | ✅ | captured |
| ListChatTurns | `khqZz` (GET_CONVERSATION_TURNS) | ✅ | captured |
| DeleteChatTurns | `J7Gthc` (DELETE_CONVERSATION) | ✅ | captured |
| GeneratePromptSuggestions | `otmP3b` (SUGGEST_PROMPTS) | ✅ | compiled |
| ShareProject | `QDyure` (SHARE_NOTEBOOK) | ✅ | captured; sharing service |
| GetProjectDetails | `JFMDGd` (GET_SHARE_STATUS) | ✅ | captured; sharing service |
| GetOrCreateAccount | `ZwVcOc` (GET_USER_SETTINGS) | ✅ | captured |
| MutateAccount | `hT54vc` (SET_USER_SETTINGS) | ✅ | compiled |

### APK-only relative to the batchexecute registry (16)

| APK method | `notebooklm-py` coverage / closest web equivalent |
|---|---|
| DiscoverSources | `research.discover()` (both backends, since #2283); the async Research family stays on `start`/`poll` |
| GenerateAccessToken | no public API |
| GenerateFreeFormStreamed | implemented through the separate streamed query endpoint, not `RPCMethod` |
| GetArtifactCustomizationChoices | `sqTeoe` (GET_CUSTOMIZATION_CHOICES) — `artifacts.get_customization_choices()` on both backends; see [copy-append-suggestion-evidence.md](copy-append-suggestion-evidence.md#getartifactcustomizationchoices) |
| GetArtifactUserState | no standalone public API |
| UpsertArtifactUserState | no standalone public API |
| GetDriveSourceStatus | no exact caller; source freshness uses `CheckSourceFreshness` |
| GetIceConfig | Live/WebRTC not implemented |
| SendSdpOffer | Live/WebRTC not implemented |
| StreamLiveSession | Live/WebRTC not implemented |
| ListExpertIntelligenceContent | no public API |
| LogInteractionEvent | telemetry intentionally not implemented |
| SubmitFeedback | no public API |
| SuggestArtifacts | no exact caller; web has report and prompt suggestions |
| CreateAccessRequest | no public API |
| PrototypeNotebookSearch | experimental discovery search not implemented |

### Conclusions from the parity probes

- **Labels and collections:** the APK is read-only (`GetLabels`), while direct mobile gRPC supports
  full CRUD and source/notebook membership add/remove. See
  [the live organization report](resource-lifecycle-and-public-qualification.md).
- **Notebook copy:** `CopyProject` is absent from the APK but fully routed. The first probe copied a
  one-source notebook. The parity probe then copied a notebook with 50 sources and 5 Studio
  artifacts; the copy matched both counts on its first read and every copied source/artifact ID was
  distinct. Controlled note/source/artifact mutations and deletes were performed only on that copy,
  which was then deleted.
- **Deep research:** the full async lifecycle is absent from the APK but supported by the mobile
  backend. See [the live report](deep-research-evidence.md).
- **Remaining proof gap:** `ShareAudio` lacks a valid-resource probe, and `GenerateArtifact` lacks a
  safely disposable failed artifact for an accepted replay. `ExportToDrive` report-to-Docs is
  live-proven with Drive read-back and exact deletion. `RefreshSource` is also live-proven on a
  stale native Google Doc; its earlier copied-URL rejection remains historical evidence only.

Detailed request shapes and the destructive-copy test log are in
[Web-parity gap probes over mobile gRPC](grpc-capability-and-signature-evidence.md).

Field *numbers* differ between the two transports (protobuf field tags on mobile vs. positional
JSON arrays on web), but the message *semantics* line up — the web `rpc/` decoders are the best
cross-check for naming mobile fields.

## Common building blocks

These shapes recur across methods. Field numbers are stable; **names below are now the real ones
recovered from the binary** (see [android/schema.proto](schema.proto)) — the earlier
`(inferred)` guesses are annotated where they differed.

### UUID string (`str[36]`)

A 36-character string is the canonical hyphenated UUID used for every entity ID:
`project_id`, `source_id`, `note_id`, `artifact_id`, and chat-session/turn IDs.

### Timestamp

`google.protobuf.Timestamp`, seen everywhere as:

```text
{ #1 varint = seconds, #2 varint = nanos }
```

Seconds are ~`1.78e9` (mid-2026), which is how the decoder recognises them.

### Client-context envelope → `RequestContext`

Almost every request carries this sub-message (`RequestContext`, from
`labs.language.tailwind.common.protos`). **Its field number differs per method** (this is pitfall
#2 in [CLAUDE.md](../../CLAUDE.md) — position-sensitive nesting). Real field names (was inferred):

```proto
message RequestContext {
  ClientType clientType = 1;                       // was "platform enum = 3 (Android)"
  ClientMetadata clientMetadata = 2;               // was "{ str[16] } session token"
  repeated ClientCapability clientCapabilities = 3; // was "str[1] flag"
  Provenance provenance = 4;                        // was "{ app/build descriptor }"
  AppApiCapabilities appApiCapabilities = 5;        // was "{ str[4] } locale"
  RequestExecutionMode executionMode = 6;
}
```

Where the context is attached per method:

| Method | context field | project/entity ID field |
|---|---|---|
| `GetOrCreateAccount` | `#1` | — |
| `GetProject` | `#3` | `#1` (+ `#2 varint = 1`, a view flag) |
| `GenerateNotebookGuide` | `#2` | `#1` |
| `GetNotes` | `#4` | `#1` |
| `ListArtifacts` | `#1` | `#2` |
| `ListChatSessions` | `#1` | `#3` |
| `ListChatTurns` | `#1` | `#4` |

`GetOrCreateAccount`, `ListChatSessions` also carry a **secondary context** at `#2`:
`{ #1 = 1, #11 { #1 = 2, #2 = 1, #3 str[16] } }`.

## Method reference

Read / response-shaped RPCs (individual rows call out stateful exceptions):

| Method | Req bytes | Resp bytes | Emitted when | Notes |
|---|---:|---:|---|---|
| `ListRecentlyViewedProjects` | variable | variable | app home | compiled; web-equivalent list live |
| `GetOrCreateAccount` | 91 | 41 | app launch | stateful account bootstrap; the private adapter does not replay |
| `GetProject` | 105 | ~21,885 | open a notebook | full notebook + sources |
| `GenerateNotebookGuide` | 101 | 1,851 | open a notebook | **stateful — do not replay** |
| `GenerateDocumentGuides` | 105 | 1,327 | open a source | per-source summary + suggested Qs |
| `ListChatSessions` | 129 | 40 | enter chat | |
| `ListChatTurns` | 101 | ~991,051 | enter chat | full chat history |
| `ListArtifacts` | 101 | 64,223 | open Studio tab | studio artifacts |
| `GetArtifact` | 103 | 257–1,604 | poll generation | one artifact + status |
| `GetNotes` | 101 | 4,460 | open Studio tab | user notes |
| `LoadSource` | 103 | ~1.2 MB | open a source | full source text + rich-text tree |
| `CheckSourceFreshness` | variable | 0 | direct parity probe | APK-absent; valid copied URL source succeeded |
| `GenerateReportSuggestions` | variable | variable | direct parity probe | APK-absent; four suggestion rows returned |

Write / mutation RPCs (see [Write RPCs](#write--mutation-rpcs) — **do not replay against real data**):

| Method | Req bytes | Resp bytes | Triggered by |
|---|---:|---:|---|
| `CreateProject` | 93 | 92 | create notebook |
| `MutateProject` | 173 | 151 | rename notebook / edit fields |
| `DeleteProjects` | 101 | 0 | delete notebook |
| `CopyProject` | variable | bare `Project` | duplicate notebook; 50-source/5-artifact live replay |
| `AddTentativeSources` | 140 | 112 | begin adding source(s) |
| `AddSources` | 194 | 253 | commit source(s) |
| `DeleteSources` | 103 | 0 | remove a source |
| `MutateSource` | variable | `MutateSourceResponse` wrapping `Source #1` | APK-absent; copied-source rename + read-back |
| `RefreshSource` | variable | `RefreshSourceResponse` wrapping `Source #1` | stale native Google Doc refreshed successfully; earlier copied-URL probe returned `INVALID_ARGUMENT` |
| `CreateLabel` / `MutateLabel` / `DeleteLabels` | variable | record set / empty | labels and collections; backend-only live replay |
| `CreateArtifact` | 199 | 198 | generate studio artifact (audio/video/…) |
| `UpdateArtifact` / `DeleteArtifact` | variable | `Artifact` / empty | copied-report rename/delete + read-back |
| `CreateNote` / `MutateNote` / `DeleteNotes` | variable | `ProjectNote` / empty | disposable note lifecycle on copied notebook |
| `GenerateArtifact` / `ExportToDrive` / `ShareAudio` | variable | mixed | retry wire + READY precondition rejection; report-to-Docs success/read-back/delete; ShareAudio invalid-ID only |
| `DiscoverSources` | 136 | 2,047 | research / "find sources from the web" |
| `GenerateFreeFormStreamed` | 476 | streamed | chat: ask the notebook (**server-streaming**) |
| `GetChatSessionStatus` | variable | status/token row | chat: read idle/generating state; Web-derived signature, mobile-live tags |
| `CancelGeneration` | variable | named empty response | chat: stop an active WEB-client-type stream; APK-exact signature |
| `DeleteChatTurns` | 103 | 0 | chat: clear history |
| `ShareProject` † | 109 | 0 | set notebook visibility |
| `GetProjectDetails` † | 101 | 157–161 | read share settings |

† on `LabsTailwindSharingService`, not the orchestration service.
Sizes are from a single account/notebook; treat as order-of-magnitude. Fixed byte counts are from
the 2026-07-22 app capture. Rows labelled direct/parity/copy are later bearer/gRPC probes on
disposable resources; their bodies vary and were not derived from an HTTP Toolkit size sample.

---

### GetOrCreateAccount

**Request** — context envelope only (`#1` = context, `#2` = secondary context). No
entity ID.

The pinned exact-package reference closure declares an empty `GetOrCreateAccountRequest`; the
captured official-app context bytes are therefore outside the semantic subset compiled by the
private adapter and are not fabricated. Because the first call may create the account, transport
replay is disabled even though the result is read-shaped.

**Response** — account configuration and quotas (all inferred):

```text
#1 {
  #2 { #1=1, #2=100, #3=50, #4=500000, #5=1 }   # (inferred) quota/limits block
  #3 { #1=1, #7=0, #9=0 }                        # (inferred) feature flags
  #4 { #1 str[1] }
  #5 { #1=0, #2=1, #3=1, #4=1, #5=0 }            # (inferred) boolean feature flags
}
```

The same `{ #1=1, #2=100, #3=50, #4=500000, #5=1 }` limits block reappears in
`GetProject` response `#11`.

---

### GetProject

**Request:**

```text
#1 str[36]        # project_id
#2 varint = 1     # (inferred) view/detail flag
#3 context        # includes extra #6 varint ∈ {0,1}
```

**Response** — the full notebook. Top-level `#1` is the Project message:

```text
#1 {
  #1 str[42]                       # (inferred) project resource name
  #2 (repeated) Source { ... }     # see below — one per source
  #3 str[36]                       # project_id
  #4 str[7]                        # (inferred) emoji/title marker
  #6 { ...settings... }            # notebook settings (see below)
  #10 { #1=1, #2=1, #3=0 }         # (inferred) flags
  #11 { #1=1, #2=100, #3=50, #4=500000, #5=1 }   # account limits (echoed)
}
```

**Source** (`#1.#2`, repeated) — inferred:

```text
#1 { #1 str[36] }        # source_id
#2 str                   # title
#3 {                     # source metadata
  #2 varint              #   (inferred) word/char count
  #3 Timestamp           #   (inferred) created
  #4 { #1 str[36], #2 Timestamp }   # (inferred) revision {id, time}
  #5 varint              #   (inferred) source-type enum (seen 3/5/8)
  #7 varint              #   (inferred) status enum (seen 1/2)
  #8 { #1 str }          #   (inferred) origin/URL or description
  #9 varint              #   (inferred) size
  #15 Timestamp          #   (inferred) last-indexed
}
#4 { #2 varint = 2 }     # (inferred) processing status
#6 str                   # (inferred) short preview
#7 str                   # (inferred) summary
#8 {                     # (inferred) embeddings/derived blob
  #1 str, #3 str,
  #4 { #1 (repeated) bytes }   # opaque vectors
}
```

**Settings** (`#1.#6`) — a flat block of boolean/enum varints plus timestamps;
observed fields: `#1 #2 #3 #6(Timestamp) #7 #8 #9(Timestamp) #13 #14 #15 #18 #19
#20 #23`. Values are 0/1 flags — names not recovered.

---

### GenerateNotebookGuide

Fired automatically when opening a notebook, even on read-only navigation. **Treat as
stateful; do not replay until decoded.**

**Request:** `#1 str[36]` (project_id) + `#2` context.

**Response** — inferred guide/suggestion payload:

```text
#1 {
  #1 { #1 str }                    # (inferred) guide summary text
  #2 { #1 (repeated) { #1 str, #2 str } }   # (inferred) topic {title, body} list
  #6 { #1 (repeated) { #1 str, #2 varint=9 } }   # (inferred) suggested prompts
}
#2 str[36]                         # project_id (echoed)
```

---

### ListChatSessions

**Request:** `#1` context, `#2` secondary context, `#3 str[36]` (project_id).

**Response:** `#1 { #1 str[36] }` — a single chat-session ID. Minimal (40 bytes).

---

### ListChatTurns

**Request:** `#1` context, `#4 str[36]` (`chat_session_id`), optional `#6` page token.

**Response** — the full chat history (~1 MB in the sample). The exact-package chat overlay admits
top-level `#1` as repeated `ChatHistoryMessage` and `#2` as `nextPageToken`. Each message combines
one user query with its generated response (captured newest-first):

```text
#1 (repeated) ChatHistoryMessage {
  #1 str                   # message_id
  #2 Timestamp             # created
  #3 int32                 # observed_event_type; raw role value
  #4 str                   # user query
  #5 ActOnSourcesResponse {
    #1 AnswerResponse {
      #1 str               # generated answer
      #3 ConversationTurnKey
      #5 TailwindDoc       # rich answer document/citations
    }
  }
}
#2 str                     # next_page_token when another page exists
```

The selected Android chat adapter follows unseen nonempty `nextPageToken` values through request
field 6, aggregates at most the caller limit, preserves the final continuation token when the
snapshot truncates, and fails loudly on a token cycle. Its decoded history view reverses the
newest-first aggregate into the Python API's oldest-first Q&A order.

---

### ListArtifacts

**Request:** `#1` context, `#2 str[36]` (project_id).

**Response** — top-level `#1` is a **repeated** Artifact:

```text
#1 (repeated) Artifact {
  #1 str[36]               # artifact_id
  #2 str                   # title
  #3 varint ∈ {2,8}        # (inferred) artifact type enum
  #4 (repeated) { #1 str[36] }   # (inferred) source_ids used
  #5 varint ∈ {3,4}        # (inferred) status enum
  #8 { ...content... }     # artifact body (report/quiz/etc.), deep rich-text tree
}
```

`#8` mirrors the chat rich-text structure (offset-keyed spans + source refs). Same
depth caveat as `ListChatTurns`.

---

### GetNotes

**Request:** `#1 str[36]` (project_id), `#4` context.

**Response** — top-level `#1` is a **repeated** Note:

```text
#1 (repeated) Note {
  #1 str[36]               # note_id
  #2 {
    #1 str[36]             # (inferred) note revision id
    #2 str                 # note body text
    #3 {                   # note metadata
      #1 varint = 1
      #2 varint            # (inferred) large counter/version
      #3 Timestamp         # (inferred) created
      #6 Timestamp         # (inferred) modified
    }
    #5 str                 # (inferred) title / source ref
  }
}
#2 Timestamp               # (inferred) list-fetched-at
```

---

### GetArtifact / GenerateDocumentGuides

`GetArtifact` polls a single artifact by ID while it generates.

**GetArtifact request:** `#1 str[36]` (artifact_id), `#2` context (+ `#6 varint = 1`).
**Response** — the Artifact message (same shape as `CreateArtifact` response below); once
generation finishes, `#7.#3`/`#7.#4`/`#7.#6` carry the produced content (audio transcript
etc.) and `#11`/`#16` carry Timestamps.

`GenerateDocumentGuides` runs when a source is opened (per-source summary + suggested
questions):

```text
request:  #1 { #1 str[36] }   # source_id, wrapped   + #2 context
response: #1 {
  #1 { #1 { #1 str[36] } }    # source_id — present on a source's FIRST response only
  #2 { #1 str }               # summary text
  #3 { #1 (repeated) str }    # main ideas / keywords (this capture guessed "suggested questions")
  #4 { }                      # always zero-length in probed responses
}
```

Two readings in this capture were settled by live probes on 2026-08-31 (issues #2276, #2278):

- Response `#1` is **optional, by call ordinal**. Reading the same source three times in a row
  returns the id on call 1 and omits `#1` from the wire on calls 2 and 3, with identical summary
  bytes and no substitute identifier anywhere in the message. Source type does *not* predict it —
  an earlier reading here that credited the split to URL-vs-text was confounded by call ordinal.
  `sources.get_guide` accordingly treats an absent echo as unlabelled rather than as a mismatch.
- Response `#3` carries **keywords / main ideas**, not suggested questions: every probed source
  returned five short noun phrases, none interrogative. The proto's own `main_ideas` name agrees.

The client sends no `#2 context` and the probe succeeded without it, so the modelled-vs-captured
gap on the request side is not load-bearing for this method. The endpoint is single-source: a
two-source request is rejected with `INVALID_ARGUMENT`. Full tables in
[proto-evidence-ledger.md](proto-evidence-ledger.md#document-guide-source-echo).

## Write / mutation RPCs

All were captured on 2026-07-22 by driving a throwaway notebook. **Do not replay these
against real notebooks** — they create, mutate, and delete data. Field names are inferred.

### CreateProject — create a notebook

```text
request:  #1 empty            # (inferred) title placeholder (empty = "Untitled")
          #4 context          # note: context at #4 here, not the usual slot
          #5 secondary-context
response: #3 str[36]          # new project_id
          #6 { ...settings flags... }
          #12 { #1 str[36] }  # project_id (wrapped)
```

### MutateProject — rename / edit notebook fields

A generic field-update RPC (rename observed). The changed value sits in a nested
mask-like path:

```text
request:  #1 str[36]                 # project_id
          #2 { #4 { #2 str } }       # (inferred) update: title at #2.#4.#2
          #3 context
response: #1 str                     # new title
          #3 str[36]                 # project_id
          #4 str                     # (inferred) emoji/marker
          #6 { ...settings + Timestamps... }
```

### DeleteProjects — delete notebook(s)

```text
request:  #1 repeated str[36]   # project_ids
          #2 context
response: <empty>      # 0 bytes on success
```

### CopyProject — duplicate a notebook

This method is absent from the inspected APK but live on the mobile gRPC host. The current web
bundle and direct mobile replay agree on the field layout:

```text
request:  #1 RequestContext (optional in the successful direct replay)
          #2 str[36] source project_id
          #3 string destination title
response: bare Project
          #1 destination title
          #3 new project_id
```

The high-coverage replay copied 50 sources and 5 Studio artifacts on its first read-back. Every
copied source/artifact UUID differed from the original. See
[the parity report](grpc-capability-and-signature-evidence.md#test-target-and-copy-fidelity).

### AddTentativeSources + AddSources — add a source (two-phase)

Adding a source is a two-step commit. `AddTentativeSources` registers a placeholder and
returns a `source_id`; `AddSources` then commits the actual content (URL observed).

```text
AddTentativeSources request:
  #1 { #1 str[7] }     # (inferred) client-generated temp/batch id
  #2 str[36]           # project_id
  #3 context   #4 secondary-context
AddTentativeSources response:
  #1 { #1 { #1 str[36] }, #2 str[7], #3 { #5 varint=0 } }   # source stub {source_id, temp_id, status}
  #3 { ... same, wrapped ... }

AddSources request:
  #1 { #3 { #1 str }         # the source payload — URL string (len 47 in sample)
        #9 { #1 str[36] } }  # tentative source_id from registration
  #2 str[36]                 # project_id
  #3 context
AddSources response:
  #1 Source { ... full source object: id, title, metadata (word count, Timestamps),
              #3.#5 type enum = 5 (URL), size, ... }   # same Source shape as GetProject
```

Batch import (from `DiscoverSources`) uses the same two RPCs with **repeated** entries:
in one capture `AddTentativeSources` req/resp were 651/1,950 bytes and `AddSources`
1,728/2,681 bytes for 10 sources.

### DeleteSources — remove a source

```text
request:  #1 { #1 str[36] }   # source_id, wrapped   + #2 context
response: <empty>             # 0 bytes on success
```

### APK-unwired source/report routes

Direct mobile-bearer calls recovered valid request shapes for `MutateSource`,
`CheckSourceFreshness`, and `GenerateReportSuggestions`. The original `RefreshSource` probe was
exhaustively shaped but rejected for copied URL sources while the web transport succeeded. A later
probe using the current constructor and a stale native Google Doc succeeded through mobile gRPC.
The exact bodies, controls, historical negative results, and corrective run are kept in
[the parity report](grpc-capability-and-signature-evidence.md#newly-recovered-successful-request-shapes)
instead of duplicating them here.

### CreateArtifact — generate a studio artifact

Generation (Audio Overview observed) is a single `CreateArtifact` that returns immediately
with status = generating; the app then polls `GetArtifact`.

```text
request:  #1 context
          #2 str[36]                 # project_id
          #3 {                       # generation spec
            #3 varint = 1            # (inferred) artifact-kind enum
            #4 { #1 { #1 str[36] } } # (inferred) source_ids to use
            #7 { #2 { #2 varint = 2, #4 { #1 str[36] }, #5 str[2] } }  # (inferred) format/lang opts
          }
response: Artifact {
  #1 str[36]                 # artifact_id
  #2 str                     # title
  #3 varint = 1              # (inferred) kind
  #4 { #1 { #1 str[36] } }   # source_ids
  #5 varint = 2              # (inferred) status = generating
  #7 { ...spec echoed... }
  #20 varint = 1
}
```

## Research / source discovery

> **Backend correction, 2026-08-27:** The APK exposes only synchronous `DiscoverSources`, but the
> same mobile gRPC service routes the full async Research lifecycle. Request/response fields,
> current bundle names, replay commands, and interception instructions are in
> [Deep Research over the mobile gRPC API](deep-research-evidence.md).

### DiscoverSources — "find sources from the web"

The mobile research entry point. Takes a natural-language query, returns a ranked list of
web sources to import (each with title, domain, and a one-line rationale). Importing the
results funnels back through `AddTentativeSources` + `AddSources`.

```text
request:  #1 { #1 str }      # query text (len 29 in sample)
          #2 context
          #3 varint = 1      # (inferred) mode/count flag
          #4 str[36]         # project_id (a notebook is created first via CreateProject)
response: #1 (repeated) {    # discovered sources
            #1 str           # title
            #2 str           # domain / short label
            #3 str           # (inferred) one-line rationale
            #4 varint = 1
          }
          #2 str             # (inferred) research topic / framing sentence
          #3 { #1 str[36] }  # project_id
```

## Sharing (LabsTailwindSharingService)

A **separate gRPC service** on the same host — path
`/labs.language.tailwind.sharing.LabsTailwindSharingService/<Method>`. Three methods
exist (`ShareProject`, `GetProjectDetails`, `CreateAccessRequest`); two were captured.

> **Path corrected 2026-08-07.** This previously read
> `google.internal.labs.tailwind.sharing.v1.LabsTailwindSharingService`, which does not exist —
> that form returns `UNIMPLEMENTED`. The binary only ever contains the `labs.language.tailwind.sharing`
> form, for all three methods. Verified live: the wrong path returns `UNIMPLEMENTED`, the correct one
> returns `GetProjectDetails` tags `[1, 3, 4, 7, 8]`. Note that a probe harness built on `httpx` cannot
> read HTTP/2 trailers, so the wrong path surfaces as an *empty response* rather than an error — which
> is how this went unnoticed.

### GetProjectDetails — read share settings

```text
request:  #1 str[36]        # project_id
          #2 context
response: #1 {              # share/owner details
            #1 str          # (inferred) owner id / share path
            #2 varint = 1
            #4 { #1 str, #2 str }   # (inferred) owner display name + email/avatar
          }
          #2 { #1 = 1 }
          #3 varint = 1000  # (inferred) access-role enum
          #4 varint = 1
          #7 { #1 = 3, #2 = 1, #3 = 1 }   # (inferred) capability flags
          #8 varint = 0
```

### ShareProject — set notebook visibility

Called on "Manage notebook access" → Save. Setting "Anyone with the link" produced:

```text
request:  #1 {
            #1 str[36]              # project_id
            #3 { #1 = 1, #2 = 0 }   # (inferred) access spec: level=1 (link), flag=0
          }
          #4 context
response: <empty>          # 0 bytes on success
```

`CreateAccessRequest` (request access to someone else's notebook) was not exercised.

## Chat / ask

### GenerateFreeFormStreamed — ask the notebook a question

The chat entry point, and the **only server-streaming RPC** seen so far. The request is a
single message; the response is a sequence of gRPC frames (30+ in one sample, 281 B → 88 KB)
each carrying a **cumulative snapshot** of the growing answer — this is why a unary-only
recorder captures nothing useful and why the shape must be read across frames.

```text
request:
  #1 (repeated) InputSource { #1 SourceId { #1 str } }  # source_ids
  #2 str                                # user_query
  #3 (repeated) ConversationEvent {     # cached local turns, when present
    #1 str                              # event text
    #3 varint = 1|2                     # USER_QUERY | GENERATED_RESPONSE
  }
  #4 context
  #5 str[36]                            # chat_session_id (follow-up; absent for new)
  #6 str[36]                            # caller-generated user_message_id
  #8 str[36]                            # project_id
  #9 varint = 1                         # QUERY_ORIGIN_CHAT_TEXT_BOX

response (streamed, each frame a fuller snapshot):
  #1 {
    #1 str                              # answer text (grows 82 B → 2.6 KB across frames)
    #3 { #1 str[36], #2 str[36], #3 varint }   # answer/turn ids
    #5 { ...rich-text answer tree: offset-keyed spans, same grammar as ListChatTurns... }
  }
  #5 bool                               # is_final_response
```

Each streamed frame re-sends the whole answer-so-far, so the **final frame is the complete
answer**; earlier frames are partial. chat uses one whole-stream deadline with no retry, accepts only
a frame whose response field `#5` declares finality, and raises `ChatResponseParseError` if EOF
arrives first. It never concatenates frames. Citations are exposed only through proven
`AnswerResponse.responseDoc` fields: `TailwindDoc.objects → DocumentObject.citation →
sourceAttribution.ingestedSource.source`, with cited paragraph text from `Citation.fragment` and
answer anchors from `TailwindDoc.body.inlineObjectLocations`. Speculative flattened citation slots
are not part of the compile closure.

### GetChatSessionStatus / CancelGeneration — inspect or stop generation

`GetChatSessionStatus` takes the chat session ID at request tag 2. Response tag 2 is status `1`
(idle) or `2` (generating); the generating response also carries an opaque token at tag 1.
`CancelGeneration` uses the APK-exact request (`RequestContext #1`, chat session ID `#2`, optional
agency session ID `#3`) and a named empty response. An unowned session preserves gRPC
`PERMISSION_DENIED` instead of being flattened into success.

Cancellation stops server emission, but an existing Web HTTP response does not close itself. Live
probes also show that Google cancels only streams whose generation request context says
`clientType=WEB` (2), not `ANDROID_APP` (3). The Android adapter therefore keeps Android
metadata/provenance while using client type 2 for this isolated generation/cancel path. Full probe
notes and provenance are in
[`chat-session-control-evidence.md`](chat-session-control-evidence.md).

### DeleteChatTurns — clear chat history

```text
request:  #1 context
          #2 str[36]     # chat session_id
          #4 varint = 1
response: <empty>        # 0 bytes on success
```

## Enums (recovered from the binary)

The AOT build did **not** strip protobuf metadata: message type names
(`AddSourcesRequest`/`Response`, `ArtifactRequest`, …) and full **enum value names** survive as
string literals and are recoverable without a decompiler:

```bash
strings -a <…flutter_artifacts.so> | grep -oE '\bARTIFACT_TYPE_[A-Z_]+\b' | sort -u
```

| Enum | Values (names as compiled) |
|---|---|
| `ArtifactType` | UNKNOWN, APP, AUDIO_OVERVIEW, EXPLAINER_VIDEO, FANTASY_MAP, FILE, INFOGRAPHIC, MINDMAP, SLIDES, TABLE, TAILORED_REPORT |
| `ArtifactStatus` | UNKNOWN, SUGGESTED, INITIALIZED, PROCESSING, READY, FAILED |
| `SourceContentType` | UNKNOWN, URL, YOUTUBE_VIDEO, PDF, TEXT, MARKDOWN, IMAGE, AUDIO, CSV, EXCEL, WORD, POWERPOINT, EPUB, DRIVE, GOOGLE_DOC/SHEET/SLIDES, GMAIL, GEMINI_CHAT, AI_MODE_CHAT, EXPERT_INTELLIGENCE |
| `SourceStatus` | UNSPECIFIED, TENTATIVE, PENDING, COMPLETE, PENDING_DELETION, ERROR |
| `ProjectRole` / `UserRole` | UNKNOWN/UNSPECIFIED, NOT_SHARED, OWNER, WRITER, READER |
| `Visibility` | VISIBLE, HIDDEN, CHILDREN_HIDDEN, REPRESSED_PRIVACY, REPRESSED_COUNTERFACTUAL |

These resolve most `(inferred)` enum tags above — e.g. the `#5 = 2` on a freshly-created
`CreateArtifact` is `ArtifactStatus = PROCESSING`, the `SourceStatus = TENTATIVE` stage matches
`AddTentativeSources`, and share `ProjectRole` covers OWNER/WRITER/READER. **Caveat:** string
order is not proto-tag order, so value→**number** assignments still need correlation with captures
or a decompiler; only names are certain from mining.

## Recovering the remaining field names (reversing the binary)

`decode_mobile_grpc.py` gives field *numbers* + wire types; semantic *names* are inferred. To
recover the real names, the binary is a viable target because field-name strings are present.
Graduated options, cheapest first:

1. **String mining (done):** enum value names + message type names (above). Free, no tooling.
2. **`blutter` (recommended for full field names):** decompiles the Dart AOT snapshot and recovers
   the generated `*.pb.dart` classes — including each message's `BuilderInfo` (field name ↔ tag ↔
   type ↔ nested message). This is what yields a near-complete `.proto`. Cost: match the app's
   Dart/Flutter version, build/run blutter against
   `libNotebookLM_prod_android_library_flutter_artifacts.so`. Version-sensitive but well-trodden.

   **This app's exact build (2026-07, app v1.46.7):**
   - Dart SDK version: **`3.13.0-256.0.dev`** (a **dev-channel** build) — Dart snapshot hash
     `80d3c83b83e625573b88d3775debfe7d`, flags `product … arm64 android compressed-pointers`.
   - Flutter engine ids (from `libflutter.so` `.rodata`, two 40-hex SHAs):
     `71947c4110b0316061390fd598fe36af5c6a07bb`, `7400c96c37b422549d1f6ea01d73126b2e8a1316`.
   - The version string is emitted by `extract_dart_info.py`; if it doesn't resolve automatically
     (dev builds aren't in the stable archive), **look it up online**: the tag exists at
     `https://github.com/dart-lang/sdk/releases/tag/3.13.0-256.0.dev`, so blutter's
     `git clone -b 3.13.0-256.0.dev` (sparse, shallow) resolves it. Cross-check the engine SHAs
     against the flutter/engine → dart-sdk `DEPS` if the tag name is ever uncertain.
   - **Snapshot layout is non-standard.** This is an add-to-app *library* build: the snapshot is a
     single **combined** blob exported as `_kDartSnapshotData` (isolate data) + `_kDartSnapshotText`
     (instructions) in **`.symtab`**, not the four standard `_kDartVm…`/`_kDartIsolate…` symbols in
     `.dynsym` that stock blutter scans for. Both symbols have `vaddr == file_offset`. Adapting
     blutter therefore needs: `extract_dart_info.py` to read the header from the renamed symtab
     symbol, and `ElfHelper.cpp::findSnapshots` to scan `.symtab` and map the combined blob into the
     isolate slots (the VM-snapshot slot is irrelevant on Dart 3.13 — see below).

   **Outcome (2026-07-22): success — full schema recovered.** blutter was ported to Dart 3.13 and
   run to completion; the recovered schema is checked in at
   [docs/android/schema.proto](schema.proto) (**295 messages, 767 fields** at the time, field
   numbers/names/types/cardinality from the binary). The port took two kinds of change, captured as
   a patch in [docs/android/blutter-dart3.13.patch](blutter-dart3.13.patch):

   - **Snapshot loading** (the novel part): `extract_dart_info.py` reads the snapshot header from the
     renamed `.symtab` symbol; `ElfHelper.cpp::findSnapshots` scans `.symtab` and maps the combined
     `_kDartSnapshotData`/`_kDartSnapshotText` blob into the isolate snapshot slots.
   - **Dart 3.13 API/stub changes** (stock blutter lags this version): `Dart_InitializeParams` dropped
     `vm_snapshot_data`/`vm_snapshot_instructions` (VM snapshot is built into the runtime now — so the
     combined blob's missing VM snapshot is a non-issue); `ObjectStore` stubs (`throw_stub`,
     `OBJECT_STORE_STUB_CODE_LIST`) were removed and migrated into VM stubs (`throw_stub` →
     `StubCode::Throw()`, ~13 `XStub → XVMStub` renames); `StubCode::HasBeenInitialized()` removed;
     closure `context`/`delayed_type_arguments` fields removed (captures inlined in 3.13). One runtime
     guard was added: an unexpected `AbstractType` class id (172) is treated as a plain `Type(cid)`
     instead of aborting, so the dump completes.

   Build with `--no-analysis` for the class/field structure (fast); build **with** analysis to
   disassemble each `BuilderInfo._i()` and recover exact tag↔name↔type — that is what
   [scripts/parse_pbschema.py](../../scripts/parse_pbschema.py) parses into the `.proto`. Every RPC
   request/response message cross-checks cleanly against the wire-capture shapes above.
3. **Dynamic (frida):** hook protobuf (de)serialization to dump field maps at runtime — **not
   viable here**, frida's native hooks crash this app (see the capture runbook's "What did not work").

Result: enums came from string-mining; the full field-named schema came from the ported blutter —
see [docs/android/schema.proto](schema.proto).

## Recovered vs. still unknown

**Full surface enumerated:** all **49 methods across 4 services** in the `1.46.7` capture binary
are known by name (see [Complete service surface](#complete-service-surface-1467-binary-snapshot))
and cross-referenced to the web `batchexecute` `rpcid`s (see
[Android ⇄ web cross-reference](#android--web-cross-reference)). The separately pinned `1.55.10`
inventory contains 53 paths and 52 exact signatures.

**Shapes recovered (high confidence):** request/response top-level shapes for **21 methods** —
the UI-reachable read/write/research/chat set plus the `LabsTailwindSharingService` pair
(`ShareProject`, `GetProjectDetails`). Entity-ID placement, `Timestamp` shape, the shared
client-context envelope, repeated-field structure, account-limits block, two-phase source-add,
generate→poll, and the server-streaming chat-answer frame model are all solid.

**Backend parity tested:** all 15 web-library methods absent from the APK reached a handler on the
mobile gRPC host. The original run had eleven valid-resource successes, three safe invalid-ID route
probes, and one routed-but-rejected `RefreshSource` result. A later stale-Google-Doc run promoted
`RefreshSource` to valid-resource success. Several APK-present note/artifact/delete methods were
also live-verified on a rich disposable copy. These later results supplement the 21 captured
shapes; they do not change the capture count.

**Field names/tags/types — recovered:** the full protobuf schema (323 messages, 868 fields) is in
[android/schema.proto](schema.proto), decompiled from the binary. This supersedes the
`(inferred)` names in the inline shapes — including every message not reachable from the mobile UI
(`CreateNote`/`MutateNote`/`DeleteNotes`, `ActOnSources`, artifact ops, the WebRTC Live messages).
The one exception is `PrototypeNotebookSearch`: the `1.55.10` build dropped that RPC and its four
`Prototype*` discovery messages, so the current file no longer carries them; their recovered names
and tags remain in the `1.46.7` schema at commit `d5df15e77`. Enum *value* names are in
[Enums](#enums-recovered-from-the-binary).

**Still approximate:** a few scalar int widths (`int32` vs `int64`) from the adder heuristic; the
deep rich-text/citation grammar inside `ListChatTurns`/`ListArtifacts`/`LoadSource`/
`GenerateFreeFormStreamed` free-text leaves (the schema names the message types, but the recursive
span/citation nesting was only sampled from captures).

To assign field names with confidence, cross-reference these numbers against strings and call
sites in `libNotebookLM_prod_android_library_flutter_artifacts.so`, or against the web
`batchexecute` decoders in `src/notebooklm/rpc/` where methods overlap.
