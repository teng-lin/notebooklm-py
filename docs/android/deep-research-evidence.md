# Deep Research over the Android gRPC API

**Status:** Mobile-backend supported; async methods are absent from the inspected Android APK

**Bundle and read path rechecked:** 2026-08-27

**Successful end-to-end mobile bearer capture:** 2026-08-13

Deep Research is callable through NotebookLM's mobile bearer/gRPC endpoint even though the
current Android application does not compile or expose its asynchronous Research client stubs.
The important distinction is:

- **APK surface:** only synchronous `DiscoverSources` and import-side
  `FinishDiscoverSourcesRun` are present.
- **mobile backend surface:** the same orchestration service also routes deep start, fast start,
  list/poll, cancel, and import.

This corrects the earlier conclusion that Deep Research was web-only. Static APK strings establish
what the installed app can call, not every method the shared backend accepts.

The original runnable mobile reproducer,
`scripts/reproduce_mobile_deep_research.py`, lived in a separate
mobile-evidence workspace and is not part of this repository.
It reads the profile's durable master token, exchanges it for a short-lived Android OAuth bearer in
memory, and uses real unary gRPC so trailer-only errors remain visible. It never prints or persists
either credential.

## Exact method names from the live web bundle

The current web bundle registers these methods under `LabsTailwindOrchestrationService`:

| web `rpcid` | web bundle method | mobile gRPC operation | role |
|---|---|---|---|
| `QA9ei` | `DiscoverSourcesAsync` | `DiscoverSourcesAsync` | start Deep Research |
| `Ljjv0c` | `DiscoverSourcesManifold` | `DiscoverSourcesManifold` | start fast Research |
| `e3bVqc` | `ListDiscoverSourcesJob` | `ListDiscoverSourcesJob` | list/poll jobs |
| `Zbrupe` | `CancelDiscoverSourcesJob` | `CancelDiscoverSourcesJob` | cancel a job |
| `LBwxtb` | `FinishDiscoverSourcesRun` | `FinishDiscoverSourcesRun` | import selected results |
| `E1lmYc` | `UpdateDiscoverSourcesStatus` | `UpdateDiscoverSourcesStatus` | set a finished job's status (4/5/6) |
| `Es3dTe` | `DiscoverSources` | `DiscoverSources` | synchronous discovery; also records a job |
| `bfVDO` | `PollSourceDiscoveryStatus` | `PollSourceDiscoveryStatus` | dead handler (`INTERNAL` on every shape, both transports) |

The full mobile path is formed without guessing:

```text
/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/DiscoverSourcesAsync
```

In the minified bundle, the stable registration is
`/LabsTailwindOrchestrationService.DiscoverSourcesAsync`; the consumer gRPC package prefix and
slash-form operation path produce the mobile path above. The method suffixes match exactly.

### Fetch, preserve, and inspect the current bundle

Run from the `notebooklm-py` repository:

```bash
NOTEBOOKLM_PROFILE=PROFILE \
uv run python scripts/capture_rpc_registry.py \
  --save-bundle /tmp/notebooklm-bundle.js \
  --json > /tmp/notebooklm-rpc-registry.json

jq '
  (.confirmed + .unmapped)
  | to_entries[]
  | select(.value.method | test("DiscoverSources"))
' /tmp/notebooklm-rpc-registry.json
```

`--save-bundle` performs the authenticated homepage discovery, downloads the public gstatic JS
chunks, saves their combined text, and analyses that exact text. By contrast,
`--bundle-file /tmp/notebooklm-bundle.js` is strictly an **offline read** of a file that already
exists; it does not download anything.

Repeat the analysis without authentication or network access:

```bash
uv run python scripts/capture_rpc_registry.py \
  --bundle-file /tmp/notebooklm-bundle.js \
  --json
```

The bundle is public JavaScript and does not contain the profile credential. The authenticated
homepage response used to discover its URL still belongs in memory only; do not capture or publish
that response with cookies.

## Recovered protobuf contract

The direct mobile calls use:

```text
host: notebooklm-pa.googleapis.com:443
service: google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService
auth: authorization: Bearer <short-lived Android OAuth token>
content type: application/grpc
```

The useful wire fields are:

| operation | request fields | response fields |
|---|---|---|
| deep start | packed fixed flag `[1]` `#2`; `ResearchQuery` `#3`; mode `5` `#4`; project ID `#5` | start-session ID `#1`; canonical run UUID `#2` |
| fast start | `ResearchQuery` `#1`; mode `1` `#3`; project ID `#4` | canonical run UUID `#1` |
| list/poll | project ID `#3` | repeated `ResearchJob` `#1` |
| cancel | canonical run UUID `#3` | empty response |
| import | run UUID `#3`; project ID `#4`; repeated `UserContent` `#5` | repeated imported source headers `#1` |

`ResearchQuery` contains query text at `#1` and source type at `#2`; web is source type `1`.
Deep Research searches web sources and uses discovery mode `5`.

The deep start IDs are not interchangeable:

- response `#1` is a 42-character start-session identifier used only as diagnostics;
- response `#2` is the canonical 36-character job UUID accepted by poll, cancel, and import.

Polling returns repeated jobs. The captured projection is:

```text
ResearchJob:
  #1 string                    canonical run UUID
  #2 ResearchJobInfo:
       #2 ResearchQuery
       #3 discovery mode       1 fast, 5 deep
       #4 ResearchResults:
            #1 repeated DiscoveredSource
            #2 summary
       #5 status               1 in progress, 2 completed, 4 cancelled
```

A URL result uses URL/title/hint/corpus at `#1/#2/#3/#4`, optional content at `#7`, and an ordinal
at `#9`. A generated deep report is a separate result row: URL `#1` is absent, its title is `#2`,
and `ResearchResultContent #7` contains Markdown at `#1` and kind `3` at `#2`.

Import encodes URL results as `UserContent.webContent #3` with URL/name at `#1/#2`. A generated
report uses `UserContent.textContent #2`, name/Markdown at `#1/#2`, plus
`textContentType=MARKDOWN(3) #4`.

The request-context fields constructed by the web client are not required on these direct mobile
calls. Successful mobile captures omitted them; the reproducer does the same rather than inventing
an Android context requirement.

## Reproducer usage

List jobs without printing notebook content or resource IDs:

```bash
cd /path/to/gemini-notebook-mobile
uv run scripts/reproduce_mobile_deep_research.py list \
  --profile PROFILE \
  --redact
```

Start Deep Research:

```bash
uv run scripts/reproduce_mobile_deep_research.py start-deep \
  "Research question" \
  --profile PROFILE
```

Persist the returned `run_id`; do not substitute `start_session_id`. Poll one run to completion:

```bash
uv run scripts/reproduce_mobile_deep_research.py wait RUN_UUID \
  --profile PROFILE \
  --wait-timeout 1800 \
  --poll-interval 5
```

Cancel and confirm the terminal state:

```bash
uv run scripts/reproduce_mobile_deep_research.py cancel RUN_UUID \
  --profile PROFILE \
  --confirm
```

Import one zero-based result, all URL results, or the generated report:

```bash
uv run scripts/reproduce_mobile_deep_research.py import RUN_UUID \
  --profile PROFILE \
  --result-index 0

uv run scripts/reproduce_mobile_deep_research.py import RUN_UUID \
  --profile PROFILE \
  --all-urls

uv run scripts/reproduce_mobile_deep_research.py import RUN_UUID \
  --profile PROFILE \
  --report
```

Import is a notebook mutation. The script re-reads the job and refuses to import until its current
status is completed. Use a disposable notebook while validating the contract.

## Live evidence and present limitation

### Successful mobile-bearer lifecycle on 2026-08-13

The independently maintained mobile evidence set records successful disposable workflows on this
same host and service:

- fast start reached status `2`, returned source candidates, imported one URL through
  `FinishDiscoverSourcesRun`, and passed `GetProject` read-back;
- deep start returned both response identifiers;
- the canonical response-`#2` UUID appeared in poll, was accepted by cancel, and reached status
  `4`;
- a second deep run reached status `2` after about five minutes, returning one report row and 40
  URL rows; and
- report import returned an imported-source header and increased notebook source count by one.

The source evidence report was named
`live-wire-validation-2026-08-13.md`; it lived in that separate workspace and
is not a stable repository link. The lifecycle observations needed by this
project are preserved in this section and in the
[`endpoints.md` research matrix](endpoints.md#research--source-discovery).

### Recheck on 2026-08-27

The current bundle still registered all five lifecycle methods. Direct mobile-bearer
`ListDiscoverSourcesJob` returned gRPC success with a valid empty response against an existing
notebook. A correctly encoded `DiscoverSourcesAsync` request reached the handler on each locally
available credentialed profile, but all returned:

```text
RESOURCE_EXHAUSTED: Resource has been exhausted (e.g. check quota).
```

This is a product quota result, not `UNIMPLEMENTED` (unknown path) or `INVALID_ARGUMENT` (bad wire
shape). It prevented a fresh completed Deep Research job on this date; it does not replace the
successful August 13 lifecycle evidence. Every disposable notebook created during the recheck was
deleted, and no job or result ID was logged.

The requested local profile was unavailable. Other credentialed profiles were checked without
printing account metadata.

### Synchronous `DiscoverSources` and `UpdateDiscoverSourcesStatus` on 2026-09-01

Both methods were probed live on the web `batchexecute` transport and on the Android bearer gRPC
transport against disposable notebooks (all deleted afterward). The two transports agree on every
shape and status code below.

**`DiscoverSources` (`Es3dTe`)** is the request the web "Discover sources" cold-start dialog sends.
It is `DiscoverSourcesRequest` exactly as recovered in `schema.proto`: `discoveryContext = 1`
(`{context = 1, corpus_type = 2}` — field 2 is a web-only addition, `1` web, `2` Drive),
`requestContext = 2` (optional), `discoveryMode = 3` (optional; absent or unknown values are
stored as `1`), `projectId = 4` (required; unknown id → `NOT_FOUND`), `clientSessionId = 6`
(accepted, no observable effect). The web positional form is `[[query, corpus], ctx, mode, nb]`.

- Modes `1`–`4` answer synchronously in 7–10 s with ten ranked web results, an overview sentence
  and `DiscoverSourcesFeedbackKey{1: job_id}`. Modes `3`/`4` ("curious") take an empty query.
- Mode `5` (deep) → `INVALID_ARGUMENT`; mode `6` (lite) → `INTERNAL`; Drive corpus → in-band error
  `1` on web and `INTERNAL` on mobile.
- Every call — successful or not — also creates a `ListDiscoverSourcesJob` row carrying the same
  result payload and the echoed mode, and the feedback key's field 1 is that row's job id. The
  synchronous method is therefore a fast-research run plus wait, and its results can be imported
  with `FinishDiscoverSourcesRun` like any other run.

**`UpdateDiscoverSourcesStatus` (`E1lmYc`)** is registered but never called by the current web
build. Its request is `{requestContext = 1 (optional), <message> = 2 (ignored), jobId = 3
(required; unknown → NOT_FOUND), status = 4}`, response `Empty`. Only status values `4`, `5` and
`6` are accepted (`0`–`3`, `7`, `8` → `INVALID_ARGUMENT`); the write is reversible and is applied
to running jobs as well as finished ones. `FinishDiscoverSourcesRun` pins the meaning of the two
unmodelled values: an empty import list moves a job to `5`, importing at least one source moves it
to `6`. Read `4` = cancelled, `5` = finished without import (dismissed), `6` = finished with
import. `CancelDiscoverSourcesJob` does not change a job already at `5` or `6`.

**`PollSourceDiscoveryStatus` (`bfVDO`)** returned `INTERNAL` on every shape tried, with nothing in
flight, with a synchronous `DiscoverSources` in flight (its `clientSessionId` at fields 2, 3, 5 and
6), with the resulting job id, and with a genuinely running Deep Research job (status `1`); the
mobile transport answers the same for `{}` and `{1: ctx, 4: project_id}`. Its web registration has
no caller and an empty response class, so it is treated as a dead handler.

Deep Research start returned `RESOURCE_EXHAUSTED` on the default profile again; a second profile
allowed two starts, both cancelled within seconds of the probes above.

## Detailed traffic interception

The full rooted-emulator setup, CA injection, UID-scoped DNAT, Mockttp recorder, and cleanup are in
[`capture.md`](capture.md). Two details matter specifically for Deep Research:

1. The current Android UI cannot originate the async methods because their stubs are absent from
   the APK. Tapping “Find sources” captures synchronous `DiscoverSources`, not
   `DiscoverSourcesAsync`.
2. To capture async Deep Research, send the direct reproducer through the same redacting Mockttp
   proxy. This host-side route was rechecked with `ListDiscoverSourcesJob` on 2026-08-27.

### 1. Prepare HTTP Toolkit trust and Mockttp

Follow steps 1–6 of [`capture.md`](capture.md#step-by-step-setup). HTTP Toolkit must have generated:

```text
~/Library/Preferences/httptoolkit/ca.pem
~/Library/Preferences/httptoolkit/ca.key
```

Install the recorder dependency outside the repository if it is not already present:

```bash
npm install --prefix /tmp/notebooklm-mockttp mockttp@4.5.0
```

### 2. Resolve a real upstream address

Avoid VPN-synthetic DNS addresses:

```bash
notebooklm_upstream_ip="$({
  curl -fsS \
    'https://dns.google/resolve?name=notebooklm-pa.googleapis.com&type=A' \
    | jq -er '.Answer[] | select(.type == 1) | .data'
} | head -1)"
test -n "$notebooklm_upstream_ip"
```

### 3. Start the redacting recorder

Use a new owner-private temporary directory and keep this terminal open:

```bash
notebooklm_capture_dir="$(mktemp -d /tmp/notebooklm-deep-research.XXXXXX)"
chmod 700 "$notebooklm_capture_dir"

cd /path/to/notebooklm-py
NOTEBOOKLM_UPSTREAM_IP="$notebooklm_upstream_ip" \
NOTEBOOKLM_CAPTURE_DIR="$notebooklm_capture_dir" \
node scripts/capture_mobile_grpc.js
```

The recorder listens on `127.0.0.1:8081`, removes authorization headers, splits gRPC frames, and
writes only metadata plus raw protobuf messages. Those protobuf messages are still private: they
contain notebook IDs, queries, URLs, titles, and generated report text.

### 4. Route the direct gRPC client through it

In a second terminal:

```bash
cd /path/to/gemini-notebook-mobile

grpc_proxy=http://127.0.0.1:8081 \
GRPC_DEFAULT_SSL_ROOTS_FILE_PATH="$HOME/Library/Preferences/httptoolkit/ca.pem" \
uv run scripts/reproduce_mobile_deep_research.py list \
  --profile PROFILE \
  --redact
```

`grpc_proxy` makes gRPC C-core use Mockttp as an HTTP CONNECT proxy.
`GRPC_DEFAULT_SSL_ROOTS_FILE_PATH` makes the client trust Mockttp's certificate for the intercepted
TLS session. Do not set either variable globally.

For a state-changing capture, substitute `start-deep`, `cancel`, or `import` and use only a
throwaway notebook. Expected recorder output has this shape:

```text
001 request ListDiscoverSourcesJob <bytes>
001 response ListDiscoverSourcesJob <bytes-or-empty>
```

### 5. Inspect without publishing raw data

Review the metadata index and decode one local protobuf when needed:

```bash
jq . "$notebooklm_capture_dir/index.jsonl"
protoc --decode_raw \
  < "$notebooklm_capture_dir/001_ListDiscoverSourcesJob.request.0.pb"
```

Do not paste decoded strings into an issue, report, or chat. Record field numbers, wire types,
cardinalities, status codes, byte sizes, and redacted identifier shapes instead.

### 6. Stop and clean up

Press `Ctrl-C` in the recorder terminal. After extracting only non-sensitive facts, remove the
temporary capture:

```bash
test -n "$notebooklm_capture_dir"
case "$notebooklm_capture_dir" in
  /tmp/notebooklm-deep-research.*) find "$notebooklm_capture_dir" -depth -delete ;;
  *) echo "refusing unexpected capture path" >&2; exit 1 ;;
esac
```

If the Android companion path was also enabled, remove the exact DNAT rule and disconnect HTTP
Toolkit as described in [`capture.md`](capture.md#stopping-and-restoring-http-toolkit).

## What did not work, and why

- Searching only the APK method table said “web-only.” That table describes compiled callers, not
  the server routing table.
- `--bundle-file bundle.js` did not download anything. It was an offline input option; the capture
  script now has an explicit `--save-bundle bundle.js` mode.
- The first tiny `httpx` probe reported an invalid gRPC frame for deep start. The actual response
  was trailers-only; `httpx` did not expose those HTTP/2 trailers. Retrying with `grpcio` surfaced
  the real `RESOURCE_EXHAUSTED` status.
- Repeating deep start on the other local mobile-token profiles produced the same quota result.
  This was not treated as a schema failure, and each disposable project was cleaned up.
- The Android app cannot be used to capture async Deep Research today because it has no UI call
  site or compiled stub for those methods. Use bundle discovery plus a direct mobile-bearer probe.
