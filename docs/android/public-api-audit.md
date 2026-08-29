# Android public-API rejection audit — 2026-08-29

This audit classifies every explicit `_reject` branch in the Android artifact, chat, mind-map,
notebook, sharing, and source adapters. It combines the latest APK method/FQN inventory, the current
web bundle (`8cc2569196b28083ba58a33319df79af97ec1832f442c4a182289894edf5eaef`), recovered mobile
message fields, retained live reports, and new Android-bearer probes against disposable notebook
copies. No credential material or resource identifiers were logged.

The audit found 44 target callsites, excluding the `_reject` helper definitions present when the
audit began. They were not all missing gRPC methods: many were local composition/download gaps or
omitted fields on RPCs that were already admitted. Every callsite has now been removed. The complete
Android namespace graph contains no `_reject` or `unsupported_operation` branch.

Where the admitted mobile contract is absent or a valid owned-resource request is demonstrably
rejected, the selected Android adapter receives a narrow Web compatibility callable. This keeps the
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
| `AddSources` `GoogleDriveContent #1` using an already-accessible Drive source | raw and public two-phase adapter both succeeded; returned source retained its Drive ID | implemented `sources.add_drive` |
| `GeneratePromptSuggestions` | success; three structurally valid suggestions | implemented `notebooks.suggest_prompts` |
| `CheckSourceFreshness` | prior and repeat valid-resource success | implemented |
| `GetDriveSourceStatus` | `UNIMPLEMENTED` (gRPC 12) | do not use as a readiness dependency |
| `RemoveRecentlyViewedProject` | `INTERNAL` (gRPC 13) | exact direct route retained for conformance; public operation delegates through the Web compatibility callable |
| `RefreshSource` | retained valid-resource `INVALID_ARGUMENT` | public operation delegates through the Web compatibility callable |
| view-level `MutateProject` branch | `PERMISSION_DENIED` (gRPC 7) on owned copy | collaborator/view-level sharing delegates through the Web compatibility API |

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
| artifact note-backed mind-map generation | Web-only `ActOnSources` then `CreateNote` | implemented through a narrow Web compatibility collaborator under Android selection; no Google FQN invented |
| interactive mind-map generation | `CreateArtifact` type 4 / app type 4; live `READY` plus direct JSON field `AppArtifact #4` | implemented in `mind_maps.generate` |

## Artifact downloads and exports

These are primarily local decoding/transfer gaps, not missing creation RPCs.

| Public operation | Evidence/status |
|---|---|
| audio / video downloads | live progressive `.googlevideo.com` transfer succeeded without forwarded credentials; MP4/`ftyp` and WAV/RIFF policies implemented, and a verified WAV response corrects a registry-derived `.m4a` destination to `.wav` |
| infographic with `artifacts_data` | normal download and typed/exact-protobuf prefetch both implemented; Web positional rows remain intentionally outside the Android contract |
| slide deck | exact PDF/PPTX fields and strict transfer implemented; current URLs still need the APK's unrecovered scoped Drive download-form token |
| report | exact rich document closure and Markdown renderer implemented; live sample covered paragraphs, table, bullets, styles, and rules |
| mind map | interactive field-4 JSON plus note-backed generation/read/download are implemented; note-backed generation uses the narrow existing Web pipeline |
| data table | live `TailwindDoc` table decoded to BOM CSV with local wire-equivalent omitted nested names |
| quiz / flashcards | exact full-`GetArtifact` app HTML/templated-app fields admitted and local JSON/Markdown/HTML saves implemented |
| report/data-table/generic export | `ExportToDrive` implemented; report-to-Docs succeeded live, exact Drive read-back matched, and exact Drive deletion returned 204; Sheets/content variants remain web-derived |

## Chat settings

`chat.configure`, inherited `chat.set_mode`, and `chat.get_settings` map to the already-live
`MutateProject` and `GetProject` methods. The APK omits the nested advanced-settings messages, so
the adapter uses repository-local wire-equivalent parsers:

- mutation `ProjectMutation #8` contains goal/custom-prompt `#1` and response-style `#2`;
- `Project.advanced_settings #8` carries the same blocks on read;
- a disposable Android mutation followed by web and Android read-back matched the requested enums
  and prompt.

These branches are implementation-ready and are implemented in this change. Missing envelopes,
partial blocks, unknown enum codes, and `CUSTOM` without a prompt fail loudly to prevent a partial
read-modify-write from clobbering settings.

## Mind maps

| Public operation | Status |
|---|---|
| note-backed rename | local composition over exact Notes read/update; implemented |
| auto-detected rename/hydration | existing note/artifact collaborators; implemented |
| auto-detected delete | existing note/artifact collaborators; implemented |
| note-backed tree and note-first auto-detection | already-decoded note JSON; implemented |
| interactive tree | live `AppArtifact #4` direct JSON with bounded `{name,children}` validation | implemented |
| interactive generation | live `CreateArtifact` type 4 / app type 4 | implemented |
| note-backed generation | narrow delegation to the existing Web `ActOnSources` + `CreateNote` pipeline | implemented under Android selection without guessing mobile fields |

## Notebooks and sources

| Public operation | Status |
|---|---|
| `notebooks.suggest_prompts` | exact APK FQNs/tags plus successful Android response; implemented |
| `notebooks.remove_from_recent` | exact APK signature but repeated valid-resource `INTERNAL`; implemented through a narrow Web compatibility callable |
| YouTube `add_url` and batch | dedicated exact `VideoContent #8`; live success; implemented |
| `sources.add_text` | exact `TextContent #2`; live success; implemented |
| `sources.add_drive` | exact `GoogleDriveContent #1`; valid existing-Drive-reference success; implemented without `GetDriveSourceStatus` |
| `sources.add_drive_file` | narrow authenticated Web download context followed by Android registration/Scotty upload | implemented without switching the namespace or upload leg to Web |
| `sources.add_file` | generic Android tentative registration/Scotty upload; a disposable text file reached ready/list read-back live | implemented for all public file types |
| `sources.refresh` | valid-resource mobile rejection | implemented through a narrow Web compatibility callable |
| `sources.check_freshness` | valid-resource Android success | implemented |

## Sharing

Public-link mutation remains native Android. The admitted Android
`ShareProject`/`GetProjectDetails` closure contains only public-document settings and does not decode
users; the owned-copy view-level mutation also returned `PERMISSION_DENIED`. Complete status,
collaborator mutations, and view-level changes therefore use the injected Web `SharingAPI`, while
the installed public namespace remains `AndroidSharingAPI`.

## Remaining compatibility seams and public assembly

Fast Drive-corpus research is native: a live `DiscoverSourcesManifold` request with
`ResearchQuery.source_type #2 = 2` returned a canonical run UUID, followed by exact cancellation and
scratch cleanup. Automatic label generation uses the injected Web callable because the mobile
organization union proves only manual labels. Account output-language/limit settings likewise use
the Web `SettingsAPI` because Android `MutateAccount` exposes unrelated consent flags.

Explicit `backend="android"` now installs Android adapters for all eleven public namespaces plus the
Android session, asset transport, and upload pipeline. `client.backends` reports the installed
adapter graph; the operation-level seams above remain explicit and tested.
