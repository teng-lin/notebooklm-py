# Android public-API rejection audit — 2026-08-29

This audit classifies every explicit `_reject` branch in the Android artifact, chat, mind-map,
notebook, sharing, and source adapters. It combines the latest APK method/FQN inventory, the current
web bundle (`8cc2569196b28083ba58a33319df79af97ec1832f442c4a182289894edf5eaef`), recovered mobile
message fields, retained live reports, and new Android-bearer probes against disposable notebook
copies. No credential material or resource identifiers were logged.

The audit found 44 target callsites, excluding the `_reject` helper definitions present when
the audit began. They were not all missing gRPC methods: many were local composition/download gaps
or omitted fields on RPCs that were already admitted. The completed implementation passes removed
27 of those callsites. The 17 retained calls are 10 artifact gaps, one note-backed mind-map branch,
three sharing operations, two source operations, and one notebook operation; Android chat now has
none.

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
Cinematic video reached `CreateArtifact` in an exploratory probe but the account returned
`RESOURCE_EXHAUSTED` (gRPC 8). More importantly, public `VideoFormat.CINEMATIC` is code 3 while the
recovered mobile enum assigns code 3 to `BREAKDOWN`; the probe therefore does not validate a
cinematic route. The public cinematic entry points remain gated before I/O. Cleanup again deleted
the copy.

## Artifact generation and mutation

| Public operation | Android mapping | Status |
|---|---|---|
| non-deep-dive audio formats | existing `CreateArtifact`; missing exact audio-format field | retain reject |
| video | existing `CreateArtifact`, exact mobile video options | implementation-ready; implemented in this change |
| cinematic video | no exact cinematic enum mapping: public code 3 conflicts with mobile `BREAKDOWN`; the only live probe ended `RESOURCE_EXHAUSTED` | retain reject before I/O |
| report / study guide | existing `CreateArtifact`, exact tailored-report options | implementation-ready; implemented in this change |
| flashcards | existing `CreateArtifact`, exact app/flashcard options | implementation-ready; implemented in this change |
| infographic | existing `CreateArtifact`, exact prompt/language/aspect/style options | implemented except unpinned `detail_level` |
| slide deck | existing `CreateArtifact`, exact prompt/language/type/length options | implementation-ready; implemented in this change |
| data table | generic `CreateArtifact` type 9; live bare request rejected because required field `Artifact #19` has no APK payload FQN | retain reject |
| revise slide | APK-exact `DeriveArtifact`; live derivation returned a new type-8 artifact and reached `READY` | implemented |
| retry failed | web `GenerateArtifact`; APK absent and invalid-ID routing only | retain reject |
| artifact note-backed mind-map generation | APK-exact outer `ActOnSources`, web-derived missing nested field-6 FQN, then `CreateNote` | retain reject |
| interactive mind-map generation | `CreateArtifact` type 4 / app type 4; live `READY` plus direct JSON field `AppArtifact #4` | implemented in `mind_maps.generate` |

## Artifact downloads and exports

These are primarily local decoding/transfer gaps, not missing creation RPCs.

| Public operation | Evidence/status |
|---|---|
| audio / video downloads | live progressive `.googlevideo.com` transfer succeeded without forwarded credentials; MP4/`ftyp` and WAV/RIFF policies implemented, and a verified WAV response corrects a registry-derived `.m4a` destination to `.wav` |
| infographic with `artifacts_data` | normal download and typed/exact-protobuf prefetch both implemented; Web positional rows remain intentionally outside the Android contract |
| slide deck | exact PDF/PPTX fields and strict transfer implemented; current URLs still need the APK's unrecovered scoped Drive download-form token |
| report | exact rich document closure and Markdown renderer implemented; live sample covered paragraphs, table, bullets, styles, and rules |
| mind map | interactive field-4 JSON generate/read/download and typed note-backed `MindMap` prefetch implemented; note-backed generation remains gated |
| data table | table payload/FQNs unresolved |
| quiz / flashcards | exact full-`GetArtifact` app HTML/templated-app fields admitted and local JSON/Markdown/HTML saves implemented |
| report/data-table/generic export | web `ExportToDrive`; only invalid-ID Android routing is retained, and valid probes create external Drive resources |

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
| note-backed generation | see `ActOnSources` nested-FQN gap above | retain reject |

## Notebooks and sources

| Public operation | Status |
|---|---|
| `notebooks.suggest_prompts` | exact APK FQNs/tags plus successful Android response; implemented |
| `notebooks.remove_from_recent` | exact APK signature but repeated valid-resource `INTERNAL`; retain reject |
| YouTube `add_url` and batch | dedicated exact `VideoContent #8`; live success; implemented |
| `sources.add_text` | exact `TextContent #2`; live success; implemented |
| `sources.add_drive` | exact `GoogleDriveContent #1`; valid existing-Drive-reference success; implemented without `GetDriveSourceStatus` |
| `sources.add_drive_file` | needs authenticated Drive download plus Android upload composition | retain reject |
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
