# Android artifact-gap validation — 2026-08-29

This run combined static analysis of the installed official NotebookLM APK with Android-bearer
gRPC probes against disposable notebook copies. It did not launch the APK UI. Claude CLI and
Karing were not used. No credential, capability URL, notebook ID, artifact ID, or artifact content
was logged. Every scratch notebook and derived artifact was removed; the final prefix sweep found
zero leaked resources, and the emulator was stopped.

## Evidence identity

- Package: `com.google.android.apps.labs.language.tailwind`
- Version: `1.46.7.940945420`
- AOT SHA-256: `082d75e36eb03aea7ea5a8c252029c48b964177311ca4ebac6392814b8e6f81f8`
- Authentication: the master credential was read from an isolated probe profile and
  exchanged in memory; neither credential was printed or persisted by the probe.

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

The decoder now projects the exact user-state closure instead of dropping it: audio playback
becomes `AudioArtifactUserState`, recognized flashcard `Struct` keys become
`FlashcardArtifactUserState`, and any other populated state is retained as
`UnknownArtifactUserState` rather than silently discarded.

The live report contained 62 structural elements: 54 paragraphs, one table, and seven horizontal
rules, plus 99 text runs, 46 paragraph-style blocks, and 35 bullet blocks. The renderer therefore
admits the exact paragraph/table/rule/style/bullet closure instead of flattening only paragraphs.

## Live mutations

`DeriveArtifact` was invoked with the APK-exact request: `RequestContext #1`, original artifact ID
`#2`, and `SlidesDerivationOptions #3` containing one slide-index/edit instruction. It returned OK
with a new type-8 artifact, progressed from `INITIALIZED` to `READY`, and exposed both PDF and PPTX
representations. This promotes `artifacts.revise_slide` from schema-only to `mobile_live`.

Interactive mind-map creation was also successful. `CreateArtifact` used outer artifact type 4 and
`AppArtifactGenerationOptions.app_type=4`, language, sources, and the steering prompt. The artifact
reached `READY`; `GetArtifact` field `AppArtifact #4` was direct UTF-8 JSON with the recursive
`{name, children}` grammar. The captured tree contained 67 valid nodes, maximum depth 3, and no
unexpected node shape. Interactive generate, tree read, and JSON download are therefore admitted.
The older note-backed `ActOnSources` generation remains gated because its web-only nested field-6
request message has no APK FQN.

## Public assembly and local representations

With explicit `backend="android"`, normal client assembly now selects `AndroidArtifactsAPI`,
`AndroidMindMapsAPI`, and `AndroidAssetDownloadService` (alongside Android Collections). The asset
service participates in client lifecycle shutdown, and the generated protobuf modules remain lazy
until an Android operation needs them. The selected mind-map surface combines interactive Studio
artifacts with the existing typed note-backed projection.

The local download surface accepts both typed Android `Artifact` objects and exact protobuf
artifacts for infographic prefetch. Note-backed mind-map prefetch accepts typed `MindMap` objects;
it does not reinterpret Web positional rows. These paths close the public prefetch overloads
without widening the Android wire contract.

## Byte-transfer boundary

Audio and video each exposed progressive, HLS, DASH, and download endpoints. The authenticated
`lh3` resolution hop retained the bearer only on exact `lh3.googleusercontent.com`; it was stripped
before the terminal host. Ordinary anonymous GETs to the progressive `.googlevideo.com` targets
succeeded: video was HTTP 200 `video/mp4` with ISO-BMFF `ftyp`, and audio was HTTP 200 `audio/wav`
with RIFF. The Android downloader now admits that exact host/MIME/signature closure and prefers the
progressive single-file representation over the download endpoint. When the registry-derived
destination has an `.m4a` suffix but the response is verified WAV (`audio/wav` plus RIFF/WAVE), the
publisher changes the final suffix to `.wav` and returns that actual path; batch downloads honor
the returned path instead of retaining stale pre-transfer metadata.

Download endpoints redirected to `drum.usercontent.google.com` and returned HTTP 403 without
credentials. No bearer was forwarded because the APK does not authorize that. Slide PDF/PPTX URLs
began on `contribution.usercontent.google.com`; anonymous access ended at sign-in HTML. The APK uses
a scoped Drive download-form choreography for those bytes, not the gRPC bearer or a SAPISID cookie.
The local slide URL/MIME/signature path is implemented and never leaks a bearer, but current live
slide retrieval remains blocked until that scoped form-token exchange is recovered.

## Negative probes and retained gates

The public enum assigns `VideoFormat.CINEMATIC` code 3, but the recovered mobile enum assigns code
3 to `BREAKDOWN`. An exploratory code-3 `CreateArtifact` reached the service and returned
`RESOURCE_EXHAUSTED`; that response neither proves cinematic semantics nor a completed artifact.
Both public cinematic entry points therefore reject before source resolution or transport I/O.

The copied data table was artifact type 9 with a required length-delimited outer field `#19`.
Inside it, field `#1` is the table document and field `#2` is generation options containing prompt
`#1` and language `#2`. A bare type-9 `CreateArtifact` with sources reached the handler but returned
`INVALID_ARGUMENT`, proving that the omitted inner payload is mandatory. APK AOT has no table
payload constructor/FQN, so generation and CSV decoding remain gated rather than assigning guessed
message names.

Non-deep audio formats are absent from the APK creation schema. Retry (`GenerateArtifact`) and
Drive export (`ExportToDrive`) are absent from the APK and retain only web-derived conventional
request/response names. Retry was not probed because no failed disposable artifact existed; no
failure was deliberately induced. Export was not probed because it would create external Drive
state.
