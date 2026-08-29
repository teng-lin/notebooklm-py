# Android public-API rejection audit — 2026-08-29

This audit classifies every explicit `_reject` branch in the Android artifact, chat, mind-map,
notebook, sharing, and source adapters. It combines the latest APK method/FQN inventory, the current
web bundle (`8cc2569196b28083ba58a33319df79af97ec1832f442c4a182289894edf5eaef`), recovered mobile
message fields, retained live reports, and new Android-bearer probes against disposable notebook
copies. No credential material or resource identifiers were logged.

The audit found 44 target callsites, excluding the `_reject` helper definitions present when
the audit began. They were not all missing gRPC methods: many were local composition/download gaps
or omitted fields on RPCs that were already admitted. The completed implementation passes removed
38 of those callsites. The six retained calls are three sharing operations, two source operations,
and one notebook operation. Android artifacts, chat, and mind maps now have no public rejection
branch.

Those six are callsites in private, currently unselected Android adapters. Explicit Android
selection still installs the Web notebook/source/sharing namespaces, so the corresponding public
methods remain available through Web. In particular, Web `sources.add_drive_file` downloads an
upload-only Drive file and feeds it through the upload pipeline; it is distinct from native
`sources.add_drive` reference ingestion and remains usable when Android artifacts are selected.

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
| `RemoveRecentlyViewedProject` | `INTERNAL` (gRPC 13) | keep rejected |
| `RefreshSource` | retained valid-resource `INVALID_ARGUMENT` | keep rejected |
| view-level `MutateProject` branch | `PERMISSION_DENIED` (gRPC 7) on owned copy | keep rejected pending mobile authorization evidence |

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
| `notebooks.remove_from_recent` | exact APK signature but repeated valid-resource `INTERNAL`; retain reject |
| YouTube `add_url` and batch | dedicated exact `VideoContent #8`; live success; implemented |
| `sources.add_text` | exact `TextContent #2`; live success; implemented |
| `sources.add_drive` | exact `GoogleDriveContent #1`; valid existing-Drive-reference success; implemented without `GetDriveSourceStatus` |
| `sources.add_drive_file` | Web supports its distinct download-then-upload workflow and remains publicly selected; the private Android source adapter would need authenticated Drive download plus Android upload composition | retain private-adapter reject |
| `sources.refresh` | valid-resource mobile rejection | retain reject |
| `sources.check_freshness` | valid-resource Android success | implemented |

## Sharing

Public visibility is already implemented. Collaborator requests are structurally recoverable from
the web bundle, but the admitted Android `ShareProject`/`GetProjectDetails` closure contains only
public-document settings and does not decode users. `set_users`, inherited add/update-user, and
`remove_user` therefore remain rejected until collaborator mutation and read-back succeed on a
disposable account pair. `set_view_level` also remains rejected after the owned-copy Android path
returned `PERMISSION_DENIED`.

## Other Android gates outside the requested files

The broader adapter still explicitly rejects non-PDF file upload, fast Drive-corpus research, and
automatic label generation. They need separate transport/schema audits; they are not silently
counted as complete by this report.

The artifact and mind-map adapters are now explicitly assembled for `backend="android"`, together
with the Android asset transport and Collections. Other partial Android adapters remain private.
This promotion makes the admitted artifact and mind-map paths reachable through the normal public
client while preserving the pre-I/O gates documented above.
