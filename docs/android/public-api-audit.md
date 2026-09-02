# Android public-API rejection audit — 2026-08-29

> **Historical snapshot; superseded for current assembly.** This report records
> the August 29 rejection audit and intentionally retains the three compatibility
> seams that existed at that checkpoint. They were closed on August 31; explicit
> Android selection now installs all eleven Android namespaces with **zero Web
> operation collaborators**. Use
> [`web-compat-seam-closure.md`](web-compat-seam-closure.md) and
> [`README.md`](README.md) for current state.

This audit classifies every explicit `_reject` branch in the Android artifact, chat, mind-map,
notebook, sharing, and source adapters. It combines the latest APK method/FQN inventory, the current
web bundle (`8cc2569196b28083ba58a33319df79af97ec1832f442c4a182289894edf5eaef`), recovered mobile
message fields, retained live reports, and new Android-bearer probes against disposable notebook
copies. No credential material or resource identifiers were logged.

The audit found 44 target callsites, excluding the `_reject` helper definitions present when the
audit began. They were not all missing gRPC methods: many were local composition/download gaps or
omitted fields on RPCs that were already admitted. Every callsite has now been removed. The complete
Android namespace graph contains no `_reject` or `unsupported_operation` branch.

At this audit snapshot, the exact backend-neutral namespace contract contained 146 consumer
callables. Under explicit `backend="android"`, their selected public paths classified as follows:

| Selected public path | Callables | Meaning |
|---|---:|---|
| Android-selected or local over Android | 143 | remains within the installed Android namespace graph, including validation, filtering, polling, aliases, cache operations, and composition over Android collaborators |
| narrow Web compatibility | 3 | `notebooks.remove_from_recent`, CSV/DOCX `sources.add_file`, and `sharing.set_view_level` |
| unsupported | 0 | no public consumer callable terminates in `_reject`, `unsupported_operation`, or `UnsupportedOperationError` |

The audit deliberately does not subdivide the first row into “native” and “local” totals. An
inherited polling or alias method can be locally implemented while terminating in an Android RPC,
and an Android-defined method can compose several other Android operations. Counting by definition
owner and counting by eventual transport therefore produce different, equally arbitrary splits;
the installed-graph/compatibility boundary above is the stable consumer-facing classification.

The count excludes the chat lifecycle hooks `reset_after_open` and `set_bound_loop`. The public root
`client.rpc_call(...)` is also outside this namespace contract: `RPCMethod` denotes Web
`batchexecute` identifiers, so that advanced raw escape hatch remains explicitly Web-specific even
when the eleven typed namespaces select Android.

At that checkpoint, where the admitted mobile contract was absent or a valid owned-resource
request was demonstrably rejected, the selected Android adapter received a narrow Web compatibility callable. This kept the
public contract available without inventing Google FQNs or silently switching the whole namespace.

## Disposable-copy live results

The Android public `notebooks.copy()` implementation copied one owned notebook to a distinct
scratch ID, including all seven source rows. All writes below targeted the copy, which was deleted
in `finally` cleanup.

| Operation | Result | Admission decision |
|---|---|---|
| `CopyProject` via `notebooks.copy()` | success; distinct ID and seven copied sources | implemented |
| `AddSources` text branch | success; one source returned | implemented `sources.add_text` |
| `AddSources` `VideoContent #8` YouTube branch | success; tentative ID echoed | implemented single/batch YouTube add |
| `AddSources` `GoogleDriveContent #1` using an already-accessible Drive source | raw and public registration/commit adapter both succeeded; returned source retained its Drive ID through exact `GoogleDocsSourceMetadata #1` with the Drive-descriptor ID as fallback | implemented `sources.add_drive` |
| `GeneratePromptSuggestions` | success; three structurally valid suggestions | implemented `notebooks.suggest_prompts` |
| `CheckSourceFreshness` | prior and repeat valid-resource success | implemented |
| `RetrieveRelevantChunks` | unfiltered and source-filtered calls returned ranked chunks with source-relative spans | implemented `sources.search` natively |
| `GetDriveSourceStatus` | `UNIMPLEMENTED` (gRPC 12) | do not use as a readiness dependency |
| `RemoveRecentlyViewedProject` | `INTERNAL` (gRPC 13) | exact direct route retained for conformance; public operation delegates through the Web compatibility callable |
| `RefreshSource` | corrected current-bundle request (`SourceId #2`, `RequestContext #3`) succeeded through Android bearer | implemented natively |
| view-level `MutateProject` branch | `PERMISSION_DENIED` (gRPC 7) on owned copy | the one sharing operation delegated through a narrow Web callable |

The completed adapters were then exercised through their real classes on another copied notebook.
Text, YouTube, freshness, prompt suggestions, and chat configure/read-back all succeeded. Artifact
kickoff returned `in_progress` tasks for flashcards, report, infographic, slide deck, and video.
Cinematic video reached `CreateArtifact`; although the account returned `RESOURCE_EXHAUSTED`, APK
UI identity proves its displayed Cinematic value is the same enum object the RPC converter emits as
protobuf value 3 (`BREAKDOWN` is a stale generated name). Cleanup again deleted the copy.

## Artifact generation and mutation

| Public operation | Android mapping | Status |
|---|---|---|
| non-deep-dive audio formats | `AudioOverviewGenerationOptions #7`; codes 2/3/4 independently accepted and echoed live | implemented with an explicit local wire overlay |
| video | existing `CreateArtifact`, exact mobile video options | implementation-ready; implemented in this change |
| cinematic video | APK UI/RPC converter identity maps displayed Cinematic to template code 3 and suppresses style | implemented; live kickoff reached the handler but quota-blocked |
| report / study guide / concept explanation | existing flexible `CreateArtifact` report strings; concept preset accepted and echoed live | implemented |
| flashcards | existing `CreateArtifact`, exact app/flashcard options | implementation-ready; implemented in this change |
| infographic | existing `CreateArtifact`; detail field `#5`, code 3 accepted and echoed live | implemented including `detail_level` |
| slide deck | existing `CreateArtifact`, exact prompt/language/type/length options | implementation-ready; implemented in this change |
| data table | `Artifact #19`, document `TailwindDoc #1`, options prompt/language `#1/#2`; generated table reached `READY` | implemented with honest local wire-equivalent nested names |
| revise slide | APK-exact `DeriveArtifact`; live derivation returned a new type-8 artifact and reached `READY` | implemented |
| retry failed | web-derived `GenerateArtifact`; request/response wire pinned, valid READY artifact rejected as non-retryable | implemented; accepted retry still lacks a disposable failed-row live fixture |
| artifact note-backed mind-map generation | current-bundle `ActOnSources` response plus exact `CreateNote` persistence | implemented natively |
| interactive mind-map generation | `CreateArtifact` type 4 / app type 4; live `READY` plus direct JSON field `AppArtifact #4` | implemented in `mind_maps.generate` |

## Artifact downloads and exports

These are primarily local decoding/transfer gaps, not missing creation RPCs.

| Public operation | Evidence/status |
|---|---|
| audio / video downloads | live progressive `.googlevideo.com` transfer succeeded without forwarded credentials; MP4/`ftyp` and WAV/RIFF policies implemented, and a verified WAV response corrects a registry-derived `.m4a` destination to `.wav` |
| infographic with `artifacts_data` | normal download and typed/exact-protobuf prefetch both implemented; Web positional rows remain intentionally outside the Android contract |
| slide deck | exact PDF/PPTX fields and strict transfer implemented; APK-exact `SSOHttpClient` + `GET` + `alr=yes` flow recovered, and the Android public API live-downloaded a valid 15,017,608-byte PDF and 17,392,113-byte OOXML PPTX through the mobile bearer |
| report | exact rich document closure and Markdown renderer implemented; live sample covered paragraphs, table, bullets, styles, and rules |
| mind map | interactive field-4 JSON plus native `ActOnSources`/`CreateNote` note-backed generation/read/download are implemented |
| data table | live `TailwindDoc` table decoded to BOM CSV with local wire-equivalent omitted nested names |
| quiz / flashcards | exact full-`GetArtifact` app HTML/templated-app fields admitted and local JSON/Markdown/HTML saves implemented |
| report/data-table/generic export | `ExportToDrive` implemented; report-to-Docs succeeded live, exact Drive read-back matched, and exact Drive deletion returned 204; Sheets/content variants remain web-derived |

## Chat settings

`chat.configure`, inherited `chat.set_mode`, `chat.get_settings`, and the
`Notebook.chat_settings` projection returned by `notebooks.get` map to the already-live
`MutateProject` and `GetProject` methods. The APK omits the nested advanced-settings messages, so
the adapter uses repository-local wire-equivalent parsers:

- mutation `ProjectMutation #8` contains goal/custom-prompt `#1` and response-style `#2`;
- `Project.advanced_settings #8` carries the same blocks on read;
- a disposable Android mutation followed by web and Android read-back matched the requested enums
  and prompt.

These branches are implemented. Missing envelopes, partial blocks, unknown enum codes, and
`CUSTOM` without a prompt fail loudly on the read-modify-write chat path to prevent clobbering;
the notebook projection follows the backend-neutral contract by reporting malformed settings as
unknown (`None`).

## Citation-rich saved chat notes

The original Android adapter used native `CreateNote` but discarded the public
`save_answer_as_note` citation inputs. The current Web bundle closes the fields omitted by the older
APK descriptor: repeated citation/source passages at `CreateNote #4`, `TailwindDoc #6`, and request
context `#7`. The adapter now builds the rich document with UTF-16 offsets and sends both citation
representations natively.

A disposable-copy live probe round-tripped the note through `CreateNote` and `GetNotes`. The clean
answer body, marked content, cited fragment, source identity/range, document objects, and
object/citation/anchor key joins all persisted. The server normalized the top-level source-passage
row to an empty placeholder, while retaining the complete citation inside the `TailwindDoc` object.
All three scratch notebooks were deleted and a final prefix sweep returned zero.

## Mind maps

| Public operation | Status |
|---|---|
| note-backed rename | local composition over exact Notes read/update; implemented |
| auto-detected rename/hydration | existing note/artifact collaborators; implemented |
| auto-detected delete | existing note/artifact collaborators; implemented |
| note-backed tree and note-first auto-detection | already-decoded note JSON; implemented |
| interactive tree | live `AppArtifact #4` direct JSON with bounded `{name,children}` validation | implemented |
| interactive generation | live `CreateArtifact` type 4 / app type 4 | implemented |
| note-backed generation | current-bundle `ActOnSources` + exact `CreateNote` | implemented natively |

## Notebooks and sources

| Public operation | Status |
|---|---|
| `notebooks.suggest_prompts` | exact APK FQNs/tags plus successful Android response; implemented |
| `notebooks.create` chat session | current live create carried exact `Project.chat_sessions #12` / session ID `#1`; projected and consumed once by the first-ask workflow |
| notebook premium capabilities | current live listing carried `Project.premium_feature_info #10` on all 18 rows with exact boolean leaves `#1/#2/#3`; projected into `Notebook.premium_features` |
| notebook chat settings | live-proven `Project.advanced_settings #8` is preserved by the full local GetProject parser and projected by `notebooks.get` |
| `notebooks.get_raw` | preserves every admitted Project field, including advanced settings, public/audio metadata, tier limits, chat sessions, premium flags, and expert-intelligence source metadata |
| `notebooks.remove_from_recent` | exact APK signature but repeated valid-resource `INTERNAL`; implemented through a narrow Web compatibility callable |
| YouTube `add_url` and batch | dedicated exact `VideoContent #8`; live success; implemented |
| `sources.add_text` | exact `TextContent #2`; live success; implemented |
| `sources.add_drive` | exact `GoogleDriveContent #1`; valid existing-Drive-reference success; implemented without `GetDriveSourceStatus` |
| `sources.add_drive_file` | Android OAuth Drive metadata/media download followed by Android registration/Scotty upload | implemented natively end to end |
| `sources.add_file` | generic Android tentative registration/Scotty upload; a disposable text file reached ready/list read-back live | implemented for all public file types |
| `sources.refresh` | corrected current-bundle request and Android-bearer success | implemented natively |
| `sources.check_freshness` | valid-resource Android success | implemented |

## Sharing

`GetProjectDetails` now decodes collaborator rows at tag `#1` as well as public settings, limits,
and policy state. `ShareProject` natively handles public links, grant/upsert batches, and removals;
the current bundle pins user email `#1`, permission `#3`, notification `#2`, request context `#4`,
and the presence-sensitive welcome-message block. No collaborator invitation was live-probed
without a controlled secondary identity, so these writes are qualified by bundle-derived exact
bytes and stateful readback tests. The owned-copy view-level mutation returned `PERMISSION_DENIED`,
making `set_view_level` the sole sharing compatibility callable.

## Compatibility seams and public assembly at the audit snapshot

Fast Drive-corpus research is native: a live `DiscoverSourcesManifold` request with
`ResearchQuery.source_type #2 = 2` returned a canonical run UUID, followed by exact cancellation and
scratch cleanup. Automatic labels are native through `CreateLabel.auto_create #5`: explicit false
labels only unlabeled sources, while true destructively regenerates all labels; live scratch probes
verified both modes and cleanup. Account output language and limits are native through
`GetOrCreateAccount` and `MutateAccount`; a live temporary language mutation/readback succeeded and
the original language was restored and verified in `finally`.

At the August 29 checkpoint, three Web compatibility operations remained:
`notebooks.remove_from_recent`, CSV/DOCX
`sources.add_file`, and `sharing.set_view_level`. Android artifact, chat, mind-map, notes, research,
settings, labels, collections, all other source operations, and all other notebook/sharing
operations are native or local composition over native Android transports.

At that checkpoint, explicit `backend="android"` installed Android adapters for
all eleven public namespaces plus the Android session, asset transport, and
upload pipeline. `client.backends` reported the installed adapter graph, while
the three operation-level seams above remained explicit and tested.

The August 31 closure later replaced all three operation collaborators; the
table and probe outcomes above remain useful as the chronology that motivated
the native recent-removal/view-level routes and Drive-staged file path.

## Known boundaries outside the public parity count

The current `1.55.10` APK contains 53 gRPC paths. Eighteen have no typed public API owner in this
library and therefore are not missing implementations of the 146-callable contract:

- notebook discovery: `BatchSearchNotebooks`, `SearchNotebooks`;
- synchronous research discovery: `DiscoverSources` is now the public `research.discover()`
  (live-verified 2026-09-01; it also records a completed job), while `research.start` keeps
  using `DiscoverSourcesManifold` or `DiscoverSourcesAsync`;
- live audio/WebRTC: `GetIceConfig`, `SendSdpOffer`, `StreamLiveSession`;
- artifact controls and state: `CancelGeneration`, `GetArtifactUserState`,
  `UpsertArtifactUserState`, `SuggestArtifacts`,
  `ListArtifactScheduledNotificationConfigs`, `UpdateArtifactScheduledNotificationConfig`;
- internal/source pickers: `GenerateAccessToken`, `GetDriveSourceStatus`,
  `ListExpertIntelligenceContent`;
- product support/telemetry: `SubmitFeedback`, `LogInteractionEvent`;
- access-request workflow: `CreateAccessRequest`.

This list is a product-surface inventory, not a proposal to expose internal telemetry, token,
WebRTC, or experimental endpoints. `GetArtifactCustomizationChoices` left it on 2026-09-01: it is
now `artifacts.get_customization_choices()` (#2283). Separately, the private
`AndroidAssetDownloadService.download_urls_batch` method still raises
`UnsupportedOperationError`; no public operation calls it because every Android artifact download
uses the typed one-representation transfer path.

The remaining implementation-evidence gaps do not cause public fallback or rejection:

- `artifacts.retry_failed` has exact wire and handler evidence, but no safely disposable failed
  artifact has yet been available to prove an accepted retry;
- cinematic-video kickoff reaches `CreateArtifact`, but the live account was quota-blocked;
- report-to-Docs export is live-proven, while Sheets and literal-content export variants remain
  covered by exact wire/unit tests rather than a Drive-side live read-back;
- collaborator grant/remove requests have exact current-bundle bytes and stateful tests, but no
  live invitation was sent without a controlled secondary identity.
