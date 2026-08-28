# Labels, collections, and notebook copy over mobile gRPC

**Status:** All operations below are routed and working on the mobile bearer endpoint

**Live validation:** 2026-08-27

The inspected Android APK exposes only `GetLabels`; it does not contain callers for
`CreateLabel`, `MutateLabel`, `DeleteLabels`, or the very new `CopyProject`. That is an APK/UI
limitation, not a server limitation. Direct calls authenticated with the Android bearer completed
source-label CRUD, notebook-collection CRUD, membership add/remove, and notebook copy.

This gives three different answers to “does mobile support it?”:

| layer | source labels | notebook collections | notebook copy |
|---|---|---|---|
| inspected APK | read only (`GetLabels`) | read only through `GetLabels` | absent |
| mobile bearer/gRPC backend | full CRUD + membership | full CRUD + membership | supported |
| current web bundle | full callers | full callers | full caller |

The runnable reproducer is
[`scripts/reproduce_mobile_organization.py`](../../../gemini-notebook-mobile/scripts/reproduce_mobile_organization.py).
It reads the selected profile's master-token record, exchanges it for a short-lived mobile bearer
in memory, and never prints either credential.

## Current method registry

The live web bundle maps the public-looking gRPC operation names exactly:

| web `rpcid` | registered method | role |
|---|---|---|
| `I3xc3c` | `/LabsTailwindOrchestrationService.GetLabels` | list source labels or collections |
| `agX4Bc` | `/LabsTailwindOrchestrationService.CreateLabel` | create either kind |
| `le8sX` | `/LabsTailwindOrchestrationService.MutateLabel` | properties and memberships |
| `GyzE7e` | `/LabsTailwindOrchestrationService.DeleteLabels` | delete either kind |
| `te3DCe` | `/LabsTailwindOrchestrationService.CopyProject` | duplicate a notebook |

The direct mobile path adds the package prefix and uses slash-form gRPC syntax, for example:

```text
/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/CopyProject
```

The same bundle also registers internal copy helpers: `CopySourcesAsync` (`R27wvc`),
`CopySources` (`Z8UXi`), `CopyArtifactsAsync` (`mKDdke`), and `CopyArtifacts` (`zVGIdd`). The
top-level UI call site uses `CopyProject`; callers should use that operation instead of attempting
to orchestrate the helpers.

## Shared response record

`GetLabels` returns the same logical record for both resource kinds:

```text
record:
  #1 string             name
  #2 repeated member    source IDs or notebook IDs
  #3 string             label/collection UUID
  #4 string             emoji
```

There is one easy-to-miss wire difference:

- a source label's `#2` members are wrapped `SourceId { #1 UUID }` messages;
- a notebook collection's `#2` members are raw UUID strings.

The top-level response also distinguishes the two: source-label records are repeated at `#1`,
while collection records are repeated at `#2`.

## Exact request fields

The request context is optional on the direct calls tested here. The reproducer omits it instead
of fabricating Android metadata.

### List

```text
GetLabels source labels:
  request  #2 project UUID
  response #1 repeated label record

GetLabels notebook collections:
  request  #3 varint = 3
  response #2 repeated collection record
```

The value `3` is the server's collection discriminator.

### Create

Both kinds use `CreateLabel` and a manual-create block at request `#6`:

```text
manual-create #6:
  #1 properties:
       #1 string name
       #2 string emoji (optional)
```

Source label additions:

```text
request #2 project UUID
        #6 manual-create
             #2 repeated raw source UUIDs (optional initial membership)
```

Notebook collection additions:

```text
request #6 manual-create
             #3 repeated raw notebook UUIDs (optional initial membership)
        #7 varint = 3
```

The create response can be decoded, but the reproducer deliberately identifies the new object by
ID-diff across `GetLabels`. Names are not unique, so name matching alone is unsafe.

### Mutate properties and membership

Source-label request prefix:

```text
#2 project UUID
#3 label UUID
#4 repeated operation
```

Collection request prefix:

```text
#3 collection UUID
#4 repeated operation
#5 varint = 3
```

Operation variants are:

| operation | operation field | nested payload |
|---|---:|---|
| set name/emoji, either kind | `#1` | name `#1`, emoji `#2` |
| add source to label | `#2` | raw source UUID `#1` |
| remove source from label | `#3` | raw source UUID `#1` |
| add notebook to collection | `#4` | raw notebook UUID `#1` |
| remove notebook from collection | `#5` | raw notebook UUID `#1` |

Although the member payload is repeated in the schema, the server currently applies only its first
ID per mutation. The reproducer therefore sends one `MutateLabel` call per distinct member. A
multi-member operation is not atomic: if a later call fails, earlier changes remain.

Membership removal only unassigns the source or notebook. It does not delete the underlying
resource.

### Delete

```text
DeleteLabels source labels:
  #2 project UUID
  #3 repeated label UUIDs

DeleteLabels notebook collections:
  #3 repeated collection UUIDs
  #4 varint = 3
```

### Copy a notebook

The live bundle's call site constructs:

```text
CopyProject request:
  #1 RequestContext (optional in the direct replay)
  #2 source project UUID
  #3 title for the new project

CopyProject response:
  bare Project
  #1 copied title
  #3 new project UUID
```

This is not merely a title-only clone. The first disposable probe copied one URL source and gave it
a new source UUID. A second parity run used the new `client.notebooks.copy(...)` implementation on
a substantially richer notebook: 50 sources and 5 Studio artifacts. The duplicate matched both
counts on the first read-back; every source UUID and every artifact UUID differed from the
original. This directly verifies source and Studio-artifact copying. Notes and chat-history copying
were not independently tested because the selected original had no notes and the audit did not
inspect private chat content.

## Live mutation matrix

Every write used disposable names and IDs. All created labels, collections, originals, and copies
were deleted afterward.

| test | result |
|---|---|
| list source labels | gRPC success |
| create, rename, and delete source label | success + `GetLabels` read-back |
| add and remove source membership | success + membership read-back |
| list notebook collections | gRPC success |
| create, rename, and delete collection | success + `GetLabels` read-back |
| add and remove notebook membership | success + membership read-back |
| copy empty notebook | success, distinct project UUID |
| copy notebook containing one URL source | success, one copied source with a distinct source UUID |
| copy notebook containing 50 sources + 5 Studio artifacts | success on first read-back; all 55 child UUIDs distinct from original |
| mutate/delete copied children | source rename/delete, report rename/delete, note create/update/delete all verified; copy cleaned up |

Empty control payloads also establish current routing behavior: `CreateLabel` returned
`INVALID_ARGUMENT`, while nonexistent mutate/delete targets returned `NOT_FOUND`. Earlier probes
using guessed bodies had reported `UNIMPLEMENTED`; that conclusion is obsolete.

## Reproducer usage

Read without disclosing names or resource IDs:

```bash
cd /Users/blackmyth/src/gemini-notebook-mobile

uv run scripts/reproduce_mobile_organization.py list-labels \
  --profile PROFILE --redact

uv run scripts/reproduce_mobile_organization.py list-collections \
  --profile PROFILE --redact
```

Create and update a source label:

```bash
uv run scripts/reproduce_mobile_organization.py create-label "Topic" \
  --emoji "🧪" --profile PROFILE

uv run scripts/reproduce_mobile_organization.py set-label LABEL_UUID \
  --name "Renamed topic" --profile PROFILE

uv run scripts/reproduce_mobile_organization.py add-sources LABEL_UUID \
  SOURCE_UUID [SOURCE_UUID ...] --profile PROFILE

uv run scripts/reproduce_mobile_organization.py remove-sources LABEL_UUID \
  SOURCE_UUID [SOURCE_UUID ...] --profile PROFILE

uv run scripts/reproduce_mobile_organization.py delete-label LABEL_UUID \
  --profile PROFILE
```

Create and update a collection:

```bash
uv run scripts/reproduce_mobile_organization.py create-collection "Research" \
  --profile PROFILE

uv run scripts/reproduce_mobile_organization.py add-notebooks COLLECTION_UUID \
  NOTEBOOK_UUID [NOTEBOOK_UUID ...] --profile PROFILE

uv run scripts/reproduce_mobile_organization.py remove-notebooks COLLECTION_UUID \
  NOTEBOOK_UUID [NOTEBOOK_UUID ...] --profile PROFILE

uv run scripts/reproduce_mobile_organization.py delete-collection COLLECTION_UUID \
  --profile PROFILE
```

Copy a notebook. This creates a real notebook and does not clean it up automatically:

```bash
uv run scripts/reproduce_mobile_organization.py copy-notebook \
  SOURCE_NOTEBOOK_UUID "Copy title" --profile PROFILE
```

Label commands default to the profile's `multi_source_notebook_id`; pass `--notebook-id` to target
another notebook. Collection commands are account-scoped and take no target notebook option.

## Capture these calls through HTTP Toolkit

The full emulator and CA-install procedure is in [capture.md](capture.md). Once its Mockttp
recorder is listening on `127.0.0.1:8081`, a host-side replay can be captured without opening the
app UI:

```bash
cd /Users/blackmyth/src/gemini-notebook-mobile

grpc_proxy=http://127.0.0.1:8081 \
GRPC_DEFAULT_SSL_ROOTS_FILE_PATH="$HOME/Library/Preferences/httptoolkit/ca.pem" \
uv run scripts/reproduce_mobile_organization.py list-collections \
  --profile PROFILE --redact
```

Filter captured paths for `GetLabels`, `CreateLabel`, `MutateLabel`, `DeleteLabels`, or
`CopyProject`. Even redacted CLI output does not redact the protobuf body in the proxy recorder;
captures contain resource IDs and user-chosen names. Keep them outside the repository unless they
have been structurally decoded and scrubbed.

## Public product confirmation

Google's current [Create a notebook in Gemini Notebook](https://support.google.com/gemininotebook/answer/16206563?hl=en)
help page documents private notebook copy and says sources plus Studio content are copied, while
notes and chat history are not. The current
[source help page](https://support.google.com/notebooklm/answer/16215270?hl=en) documents automatic
and manual source labels. Neither page establishes Android UI support; the live calls above are the
evidence for the mobile backend specifically.
