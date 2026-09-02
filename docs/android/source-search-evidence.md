# Source chunk search evidence

`SourcesAPI.search()` exposes NotebookLM's ranked passage retrieval on both transports. This note
records the 2026-09-01 live probes that admit Web RPC `ASU5Oe` and the native Android gRPC route.

## Wire layout

The Web call to `ASU5Oe` (`RetrieveRelevantChunks`) succeeded with:

```text
[project_id, query, null, [1]]
[project_id, query, null, [1], [[[source_id], ...]]]
```

The first form searches all notebook sources; the second filters by source id. The decoded Web
reply is an outer response wrapper containing repeated source groups:

```text
[[[source_id, [chunk, ...]], ...]]
chunk = [[[[text_part, ...]]], rank, [[null, start, end], ...]]
```

A direct bearer-authenticated Android call then succeeded at:

```text
/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/
RetrieveRelevantChunks
```

The Android request and response tags match the Web positions exactly:

| Message | Live-populated fields |
|---|---|
| `RetrieveRelevantChunksRequest` | `project_id #1`, `query #2`, `options #4`, `source_filter #5` |
| `RetrieveRelevantChunksOptions` | `mode #1 = 1` |
| `SourceIdFilter` | repeated `SourceId #1` |
| `RetrieveRelevantChunksResponse` | repeated source groups `#1` |
| source group | `source_id #1`, repeated chunks `#2` |
| chunk | content `#1`, global rank `#2`, repeated spans `#3` |
| content/text | text wrapper `#1`, repeated string parts `#1` |
| span | start `#2`, end `#3` |

Both unfiltered and single-source-filtered Android calls returned ranked chunks. Rank is global
across sources and lower is better. Span start `0` is omitted by proto3 JSON rendering but is
present as the scalar default after protobuf decoding; the public type therefore returns `(0,
end)` rather than treating it as a missing span. The checked-in Android gRPC cassette replays the
same public-client calls.

The route and conventional request/response type names are not in the captured app's eager
generated method closure. Their admission is a `web_mobile_overlay`: Web establishes the semantic
name and positional layout, while the successful native calls pin the Android route, cardinality,
field tags, and response behavior.

## Drift monitoring

`ASU5Oe` is registered by a lazy source-search frontend module and is absent from the authenticated
homepage's eager JavaScript bundle set. The registry scraper therefore classifies it separately as
`LAZY-MODULE`, not `ABSENT`. `scripts/check_rpc_health.py` sends a direct read-only request, so an
RPC-id rotation remains a live failure instead of being hidden by that classification.
