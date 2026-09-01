# Copy, append and suggestion RPCs over Android gRPC

**Status:** Active
**Last Updated:** 2026-09-01

Live Android gRPC evidence for the six #2283 RPCs the Web front door serves as
`X1snv` / `QsNTEd` / `R27wvc` / `mKDdke` / `OcvKNc` / `sqTeoe`. Every call below
went to `notebooklm-pa.googleapis.com` over a raw `grpc.aio` channel with the
profile's native Android bearer (the shipped `BearerProvider`), hand-encoded
protobuf and the shipped `RequestContext` serializer, so the status codes and
`grpc-message` details are the server's own — not `AndroidSession`'s collapsed
`GrpcStatus`. Requests are written tag-first (`{tag: value}`); replies are the
decoded wire trees with identifiers shortened.

Method paths are all on
`/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/`.

| Method | In the official APK | Android bearer | Web (`batchexecute`) |
|---|---|---|---|
| `AddSourcesAsync` | no | **served** — see below | served (`X1snv`) |
| `AppendSource` | no | **served** | served (`QsNTEd`) |
| `CopySourcesAsync` | no | **served** | served (`R27wvc`) |
| `CopyArtifactsAsync` | no | **served** | served (`mKDdke`) |
| `NextStepSuggestions` | no | **served** | served (`OcvKNc`) |
| `GetArtifactCustomizationChoices` | yes (`1.55.10`) | **served** | served (`sqTeoe`) |

"Not in the APK" means the official app compiles no caller — it says nothing
about the backend, which routes all six to the mobile bearer. The scratch
notebook used for the writes (`grpc-probe-scratch-2283`, one pasted-text seed
source) was created and deleted for this probe; the copy sources came from the
maintainer's `E2E-MultiSource` / `E2E-Generation` notebooks, which were only
read.

## Correction: `AddSourcesAsync` is not blocked on mobile

[`web-compat-seam-closure.md`](web-compat-seam-closure.md) records
`CredsPermissionException: Rejected by impersonation policy for
/…AddSourcesAsync` and concludes the method is "internal to the Web frontend".
That failure was produced by the **Web upload-finalize path** (`notebooklm.google.com`)
when handed an Android bearer — it is the Web front door refusing to impersonate
a mobile credential. The native gRPC route accepts the same bearer and commits
(see [AddSourcesAsync](#addsourcesasync)). The seam-closure conclusion about
Word uploads stands; only the "no mobile route" inference was wrong.

## AddSourcesAsync

Request is the exact `AddSourcesRequest` shape the app already sends to
`AddSources` — `UserContent` #1 (here `WebContent{url = 1}` at #3), `project_id`
#2, `RequestContext` #3 — with **no** tentative-source correlation step.

```text
request  {1: [{3: {1: "https://example.com/"}}], 2: <scratch project>, 3: <RequestContext>}
status   OK, 140 bytes

reply
  1: Source{ 1: SourceId{1: "c505325c-…"}, 2: "https://example.com/", 3: SourceMetadata{5: 5} }
  3: { 1: Source{ …same stub… }, 2: 0 }
```

The reply carries the queued stub rows at #1 (id, url, `original_source_content_type
= 5`; no word count or ingest timestamps) and a per-source acknowledgement at #3
pairing each `Source` with an `int32` status (`0` on every observation; non-zero
meanings unrecovered). Field #2 was absent. A `sources.list` a few seconds later
showed the row `READY` with the title `Example Domain`. This is byte-for-byte the
positional layout the Web front door returns (`[[Source…], null, [[Source, 0]…]]`).

Generated: `AddSourcesAsync(AddSourcesRequest) returns (AddSourcesAsyncResponse)`
with `AddSourcesAsyncResponse { repeated Source sources = 1; repeated
SourceAcknowledgement acknowledgements = 3 }` and `SourceAcknowledgement { Source
source = 1; int32 status = 2 }`. The response type name is inferred (the Web
registration names it); the wire shape is live-pinned.

## AppendSource

```text
request  {2: SourceId{1: <seed source>}, 4: {2: {1: "Probe header", 2: "\n\nAPPENDED-MARKER-2283"}}}
status   OK, 0 bytes

LoadSource(seed) before:  4512 bytes, plain text 61 chars
LoadSource(seed) after:   4590 bytes, plain text 86 chars ending "…lazy dog.\n\n\n\n\nAPPENDED-MARKER-2283"
```

`AppendSourceRequest { SourceId source_id = 2; SourceContent content = 4 }`,
`SourceContent { PlainTextSourceContent plain_text = 2 }`, and the existing
`PlainTextSourceContent { header = 1; body = 2 }`. The `body` lands at the very
end of the fulltext; the `header` does not appear in it. Success is an empty
reply, so the generated signature returns `google.protobuf.Empty`. The doubly
nested content is load-bearing: `{2: SourceId, 4: {2: text}}` drew `INTERNAL`
and `{2: SourceId, 4: "text"}` drew `INVALID_ARGUMENT` on the earlier #2283
mobile ladder.

## CopySourcesAsync

```text
request  {3: [SourceId{1: "04239f2d-…"}], 4: <scratch project>}
status   OK, 154 bytes

reply
  1: CopiedSource{
       1: SourceId{1: "04239f2d-…"}                       # the original
       2: Source{ 1: SourceId{1: "eb0e0cca-…"}, 2: "Python Programming",
                  3: SourceMetadata{5: 8, 18: {…}}, 4: SourceSettings{2: 1} }
     }
```

`CopySourcesAsyncRequest { repeated SourceId source_ids = 3; string
target_project_id = 4 }` (fields 1–2 unused); `CopySourcesAsyncResponse {
repeated CopiedSource copied_sources = 1 }` with `CopiedSource { SourceId
source_id = 1; Source source = 2 }`. The copy (`eb0e0cca-…`) was present and
`READY` in the target's source list. Unknown ids are answered with a status, not
an empty mapping (follow-up probe, same day): a bogus **source id** and a bogus
**target project** both draw `NOT_FOUND` (`{3: [SourceId{1: <uuid4>}], 4: <real
target>}` and `{3: [<real source>], 4: <uuid4>}`), so an empty mapping on a
successful reply is a defensive not-found case, not the documented one. Positionally this is the Web
`[null, null, [[id]], target]` request and `[[[[orig], [[new], title, …]]]]`
reply. The synchronous twin `CopySources` (`Z8UXi`) is dead on both front doors
(`INTERNAL` on mobile for every input once its required #3 string is supplied)
and is not modelled.

## CopyArtifactsAsync

```text
request  {1: <RequestContext>, 2: ["e5f24bc2-…"], 3: <scratch project>}
status   OK, 8524 bytes

reply
  1: CopiedArtifact{
       1: "e5f24bc2-…"                                     # the original
       2: Artifact{ 1: "af713f69-…", 2: "ML Quiz", 3: 4 (APP), 4: [ArtifactSource…],
                    5: 1 (INITIALIZED), 10: AppArtifact{…} }
     }
```

`CopyArtifactsAsyncRequest { RequestContext request_context = 1; repeated string
artifact_ids = 2; string target_project_id = 3 }` — artifact ids are bare
strings, and moving the target to #4 draws `INVALID_ARGUMENT`.
`CopyArtifactsAsyncResponse { repeated CopiedArtifact copied_artifacts = 1 }`,
`CopiedArtifact { string source_artifact_id = 1; Artifact artifact = 2 }`. The
copy (`af713f69-…`, "ML Quiz", `completed`) appeared in the target's artifact
list. Bogus ids do **not** draw `NOT_FOUND`: `{1: ctx, 2: ["<uuid4>"], 3:
target}` answered `OK` with the id echoed under a separate tag-2 entry
(`{1: "<uuid4>", 3: {1: ""}}`) and no new row, so the adapter treats an empty
`copied_artifacts` mapping as not-found rather than trusting the status. A bogus
**target project**, by contrast, draws `NOT_FOUND` (`{1: ctx, 2: [<real
artifact>], 3: <uuid4>}`), so the two failure modes are distinguishable: an
unknown target is a status, unknown artifact ids are an empty mapping. The
synchronous twin `CopyArtifacts` (`zVGIdd`) validates arity, ignores the ids
and copies nothing while reporting success; it is deliberately not modelled.

## NextStepSuggestions

```text
{2: <E2E-MultiSource project>}                       OK, 218 bytes
  1: NextStep{1: "How do the different types of machine learning solve problems?", 2: 9}
  1: NextStep{1: "Explain how Python bridges the gap between programming and development.", 2: 9}
  1: NextStep{1: "What are the essential building blocks for modern web applications?", 2: 9}
{1: <RequestContext>, 2: <project>}                  OK (context accepted, not required)
{2: <project>, 3: [InputSource{1: SourceId{1: <Python source>}}]}
                                                     OK — three Python-only questions
{2: <project>, 3: [SourceId{1: …}]}                  INVALID_ARGUMENT
{2: <project>, 3: "<source id>"}                     INVALID_ARGUMENT
{}                                                   INVALID_ARGUMENT
{2: "<uuid4>"}                                       NOT_FOUND
```

`NextStepSuggestionsRequest { string project_id = 2; repeated InputSource
sources = 3 }`; the reply is the exact `NextStepSuggestions` message
(`repeated NextStep next_steps = 1`, `NextStep { suggestion = 1;
MagicArtifactType suggestion_type = 2 }`) already recovered from the APK for
chat and notebook-guide responses. Every row carried type `9`
(`CONVERSATIONAL_TEXT_CHIP`). Scoping by `InputSource` at #3 changed the
questions to the scoped source's topic (Python), so the field is honoured. Output
text is model-nondeterministic run to run. Web positional: `[None, project_id,
[[[id]], …]]` → `[[[question, 9], …]]`.

## GetArtifactCustomizationChoices

```text
{1: <RequestContext>, 2: <project>}                  OK, 3238 bytes
{1: <RequestContext>, 2: <project>, 3: <ArtifactType 1|2|3|4|6|7>}
                                                     OK, 3238 bytes (identical)
{2: <project>}                                       OK, 3238 bytes (identical)
{}                                                   OK, 3238 bytes (identical)
{1: <RequestContext>, 2: "<uuid4>"}                  OK, 3238 bytes (identical)
```

The table is account-level: `project_id` and `artifact_type` are ignored (a
bogus project id and an empty request return the same bytes), and the Web front
door behaves the same (`[]` → the same 3302-byte JSON). Reply structure:

```text
1: ArtifactCustomizationChoices{
     1: FormatChoices{ 1: {1: 1, 2: "Deep Dive", 3: "A lively conversation …"},
                       1: {1: 2, 2: "Brief", …}, 1: {1: 3, 2: "Critique", …}, 1: {1: 4, 2: "Debate", …} }
     2: FormatChoices{ 1: {1: 1, 2: "Explainer", …}, {1: 2, 2: "Brief", …},
                       {1: 3, 2: "Cinematic", …}, {1: 4, 2: "Short", …} }
     3: SlidesCustomizationChoices{ 1: SlidesType{1: 1, 2: "Detailed Deck", 3: …},
                                    1: SlidesType{1: 2, 2: "Presenter Slides", 3: …} }
     4: TailoredReportCustomizationChoices{
          1: TailoredReportTypeOption{1: "Briefing Doc", 2: "Key insights and important quotes", 3: "Create a comprehensive briefing document …"},
          1: {1: "Study Guide", 2: "Quiz with answer key plus glossary", 3: "You are a highly capable research assistant …"},
          1: {1: "Blog Post", 2: "Informational article in the style of a blog post", 3: "Act as a thoughtful writer …"} }
   }
```

Tags 3 and 4 match the APK-recovered `SlidesCustomizationChoices` /
`TailoredReportCustomizationChoices` exactly. Tags 1 and 2 — the audio and video
families — are **not declared in the APK schema** (`ArtifactCustomizationChoices`
there has only fields 3 and 4); they are live-only and generated here as
`FormatChoices { repeated FormatChoice choices = 1 }` with `FormatChoice {
int32 format = 1; string title = 2; string description = 3 }`. The served codes
match the client's `AudioFormat` (1 Deep Dive · 2 Brief · 3 Critique · 4 Debate),
`VideoFormat` (1 Explainer · 2 Brief · 3 Cinematic · 4 Short) and
`SlideDeckFormat` (1 Detailed Deck · 2 Presenter Slides) enums; `VideoFormat 5` /
`AudioFormat 5`, declared in the Web bundle, are not offered to this account. No
`VideoStyle` table appears. This is the shape the former `scripts/check_rpc_health.py`
`sqTeoe` cohort tripwire (#2284) should have sent — it sent
`[ctx, None, 3]`, which draws `INVALID_ARGUMENT`.
