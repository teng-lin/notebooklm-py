# Android public-API rejection audit — 2026-08-29

This audit classifies every explicit `_reject` branch in the Android artifact, chat, mind-map,
notebook, sharing, and source adapters. It combines the latest APK method/FQN inventory, the current
web bundle (`8cc2569196b28083ba58a33319df79af97ec1832f442c4a182289894edf5eaef`), recovered mobile
message fields, retained live reports, and new Android-bearer probes against disposable notebook
copies. No credential material or resource identifiers were logged.

The audit found 44 target callsites, excluding the six `_reject` helper definitions present when
the audit began. They were not all missing gRPC methods: many were local composition/download gaps
or omitted fields on RPCs that were already admitted. This implementation pass removed 17 of those
callsites. The 27 retained calls are 18 artifact gaps, three mind-map branches, three sharing
operations, two source operations, and one notebook operation; Android chat now has none.

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
Cinematic video reached `CreateArtifact` but the account returned `RESOURCE_EXHAUSTED` (gRPC 8), so
its wire route was accepted but quota prevented a successful kickoff in this run. Cleanup again
deleted the copy.

## Artifact generation and mutation

| Public operation | Android mapping | Status |
|---|---|---|
| non-deep-dive audio formats | existing `CreateArtifact`; missing exact audio-format field | retain reject |
| video / cinematic video | existing `CreateArtifact`, exact mobile video options | implementation-ready; implemented in this change |
| report / study guide | existing `CreateArtifact`, exact tailored-report options | implementation-ready; implemented in this change |
| flashcards | existing `CreateArtifact`, exact app/flashcard options | implementation-ready; implemented in this change |
| infographic | existing `CreateArtifact`, exact prompt/language/aspect/style options | implemented except unpinned `detail_level` |
| slide deck | existing `CreateArtifact`, exact prompt/language/type/length options | implementation-ready; implemented in this change |
| data table | generic `CreateArtifact` type 9, but inner table payload/FQNs unresolved | retain reject |
| revise slide | APK-exact `DeriveArtifact`; no valid mobile mutation capture | medium follow-up |
| retry failed | web `GenerateArtifact`; APK absent and invalid-ID routing only | retain reject |
| artifact mind-map generation | APK-exact outer `ActOnSources`, web-derived missing action field, then `CreateNote` | retain until valid mobile generation capture |

## Artifact downloads and exports

These are primarily local decoding/transfer gaps, not missing creation RPCs.

| Public operation | Evidence/status |
|---|---|
| audio / video downloads | exact media URLs are decoded; Android downloader is currently PNG-only and media host/MIME/auth policy is not live-pinned |
| infographic with `artifacts_data` | normal Android download already works; only the cross-backend raw-prefetch overload is rejected |
| slide deck | exact PDF URL is known; PPTX field and generalized transfer need admission |
| report | exact rich report document exists in recovered schema; protobuf-to-Markdown renderer is missing |
| note-backed mind map | local note JSON is available; interactive app tree payload remains unresolved |
| data table | table payload/FQNs unresolved |
| quiz / flashcards | exact app HTML/templated-app fields exist in recovered schema but are omitted from the current Android decoder |
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
| interactive tree | app payload field/JSON grammar unresolved | retain reject |
| generation | see `ActOnSources` gap above | retain reject |

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

Finally, most of these adapters are still private and intentionally absent from normal client
assembly. Removing a method-level rejection proves an adapter implementation; it does not by itself
promote the whole namespace for `backend="android"`. Namespace promotion requires its own complete
manifest, conformance run, and explicit assembly change.
