# Android resource lifecycle and public qualification

**Status:** Consolidated live evidence for notebook copy, notebook metadata, sources, notes and
note-backed mind maps, source labels, and notebook collections

**Validation window:** 2026-08-27 through 2026-08-29

> **Later implementation update (2026-08-31):** this document preserves the
> validation-window results, including negative probes against owned notebooks.
> Follow-up qualification closed the remaining operation seams; current Android
> assembly has no Web operation collaborators. See
> [`web-compat-seam-closure.md`](web-compat-seam-closure.md).

This document consolidates four dated Android-bearer reports into one stable resource-lifecycle
record. It distinguishes backend routing and wire behavior from public adapter selection. The
probes used isolated temporary profiles, minted short-lived bearers in memory, omitted account and
resource identities from output, and confined mutations to disposable resources.

## Source provenance

The original reports are identified here by their exact checked-in bytes before consolidation:

| Original report | Date | SHA-256 | Evidence retained here |
|---|---|---|---|
| `labels-collections-copy-mobile-grpc-2026-08-27.md` | 2026-08-27 | `a8bea49943cc962ed9d21d9b1ca18acbf7405fc36b1bc839090b7cfc072c5b62` | organization wire tags, heterogeneous member IDs, copy fidelity, mutation atomicity, reproducer and capture guidance |
| `notebooks-live-validation-2026-08-28.md` | 2026-08-28 | `14d74b35a28a936ca90a2a409a4bb5fd94f364bbb64b14d00177f60c38cdebeb` | authentication control, emoji set/clear/read-back, recent-removal result, cleanup |
| `notes-mind-maps-live-validation-2026-08-28.md` | 2026-08-28, extended 2026-08-29 | `53510a6cc807dcc8f1f652039e667190763c5c08a4debe64bc52bbf8bf8825f3` | cross-backend classifier, kind-safe deletion, raw compatibility, soft-delete boundary |
| `organization-live-validation-2026-08-29.md` | 2026-08-29 | `5f613c1614410216c65fc6009c6c2517ccf3bedaa3d6bf6264f8a22d84d28b4c` | later valid-resource precedence, complete CRUD/membership lifecycles, public Collections qualification |

The 2026-08-29 valid-resource organization run supersedes older rejected candidate shapes only for
the exact manual operations it exercised. It does not promote AI label generation or turn
repository-local field names into recovered Google protobuf names.

## Current method registry

The inspected APK exposes `GetLabels` but does not contain callers for `CreateLabel`,
`MutateLabel`, `DeleteLabels`, or `CopyProject`. Direct Android-bearer calls prove those methods are
routed by the mobile backend even though the APK does not expose them.

| Web RPC ID | Registered method | Role |
|---|---|---|
| `I3xc3c` | `/LabsTailwindOrchestrationService.GetLabels` | list source labels or collections |
| `agX4Bc` | `/LabsTailwindOrchestrationService.CreateLabel` | create either resource kind |
| `le8sX` | `/LabsTailwindOrchestrationService.MutateLabel` | mutate properties or membership |
| `GyzE7e` | `/LabsTailwindOrchestrationService.DeleteLabels` | delete either resource kind |
| `te3DCe` | `/LabsTailwindOrchestrationService.CopyProject` | duplicate a notebook |

The mobile path adds the exact package and slash-form gRPC syntax, for example:

```text
/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/CopyProject
```

The bundle also registers `CopySourcesAsync`, `CopySources`, `CopyArtifactsAsync`, and
`CopyArtifacts`. The product call site uses `CopyProject`. The two `*Async` helpers are live and
now backed by `sources.copy()` / `artifacts.copy()` on both backends (#2283); the two synchronous
twins are dead (`CopySources`) or a no-op stub (`CopyArtifacts`) and stay unmodelled — see
[copy-append-suggestion-evidence.md](copy-append-suggestion-evidence.md).

## Notebook copy fidelity

`CopyProject` has this live request/response contract:

```text
request:
  #1 RequestContext (optional in the direct replay)
  #2 source project UUID
  #3 destination title

response: bare Project
  #1 copied title
  #3 new project UUID
```

The first disposable probe copied an empty notebook. A second copied one URL source and allocated
a distinct source ID. The parity run then copied a notebook containing 50 sources—44 web pages,
four PDFs, one CSV, and one Markdown source—and five Studio artifacts: audio, report, data table,
infographic, and slide deck.

The rich copy returned all 50 sources and all five artifacts on its first read-back. Source-ID and
artifact-ID intersections with the original were both empty, proving that the operation copies
source and Studio content while allocating new child identities. Notes and chat history were not
independently tested because the selected original had no notes and private chat content was not
inspected.

### Copy a notebook

The reproducer creates a real notebook and does not clean it up automatically:

```bash
cd ../gemini-notebook-mobile

uv run scripts/reproduce_mobile_organization.py copy-notebook \
  SOURCE_NOTEBOOK_UUID "Copy title" --profile PROFILE
```

Google's product documentation independently says that private notebook copies include sources and
Studio content but exclude notes and chat history. The live calls above, rather than the help page,
are the evidence for the mobile backend.

## Shared response record

`GetLabels` returns one logical record for both resource kinds:

```text
record:
  #1 string             name
  #2 repeated member    source IDs or notebook IDs
  #3 string             label/collection UUID
  #4 string             emoji
```

The member representation is heterogeneous:

- a source label wraps each field-`#2` member as `SourceId { #1 UUID }`;
- a notebook collection stores each field-`#2` member as a raw UUID string;
- source-label records are repeated at top-level response field `#1`; and
- collection records are repeated at top-level response field `#2`.

## Exact request fields

Request context was optional on the successful direct calls. The reproducer omitted it instead of
fabricating Android metadata.

### List

```text
GetLabels source labels:
  request  #2 project UUID
  response #1 repeated label record

GetLabels notebook collections:
  request  #3 varint = 3
  response #2 repeated collection record
```

The value `3` is the collection discriminator.

### Create

Both kinds use `CreateLabel` with a manual-create block at request field `#6`:

```text
manual-create #6:
  #1 properties:
       #1 string name
       #2 string emoji (optional)
```

Source labels add the project UUID at request field `#2` and optional repeated raw source UUIDs at
`manual-create #6/#2`. Collections add optional repeated raw notebook UUIDs at `#6/#3` and the
collection discriminator at request field `#7`.

The create response returns source-label rows at top-level field `#2` and notebook-collection rows at
top-level field `#3`. A later authenticated collection create pinned each field-`#3` row as name
`#1`, repeated raw notebook UUID strings `#2`, collection UUID `#3`, and emoji `#4`. The public
adapter now decodes the exact returned row and requires one result, so concurrent account-level
creation cannot confuse it through a pre/post `GetLabels` ID diff. Names are not unique, so name
matching alone remains unsafe. An empty control payload returned `INVALID_ARGUMENT`.

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

| Operation | Operation field | Nested payload |
|---|---:|---|
| set name/emoji, either kind | `#1` | name `#1`, emoji `#2` |
| add source to label | `#2` | raw source UUID `#1` |
| remove source from label | `#3` | raw source UUID `#1` |
| add notebook to collection | `#4` | raw notebook UUID `#1` |
| remove notebook from collection | `#5` | raw notebook UUID `#1` |

Although a membership payload appears repeated, the server applies only its first ID per
mutation. The implementation therefore sends one `MutateLabel` call per member. A multi-member
public operation is intentionally non-atomic: if a later RPC fails, earlier mutations remain.
Membership removal only unassigns the source or notebook; it never deletes the underlying
resource. Nonexistent mutation targets returned `NOT_FOUND`.

### Delete

```text
DeleteLabels source labels:
  #2 project UUID
  #3 repeated label UUIDs

DeleteLabels notebook collections:
  #3 repeated collection UUIDs
  #4 varint = 3
```

Nonexistent delete targets returned `NOT_FOUND`.

## Read modes

The later valid-resource run reconfirmed both exact `GetLabels` modes:

| Resource | Request | Response projection |
|---|---|---|
| Source labels | owned notebook UUID at request field `#2` | repeated label records at response field `#1` |
| Notebook collections | collection discriminator `3` at request field `#3` | repeated collection records at response field `#2` |

The source-label read used a current owned notebook rather than a stale profile fixture that
correctly returned `NOT_FOUND`. The replacement notebook and account-level collection control each
started with an empty set.

## Disposable source-label lifecycle

Two disposable labels exercised the complete manual lifecycle:

1. `CreateLabel` created an empty uniquely named label and returned its canonical ID in
   `label_and_sources #2`.
2. `MutateLabel` changed its name and emoji in one property operation; read-back matched both.
3. `DeleteLabels` removed that exact ID; a final read returned an empty set.
4. A second create added one existing source member.
5. One-member `MutateLabel` added a second source while preserving the first.
6. A second one-member mutation removed the first while retaining the second.
7. `DeleteLabels` removed the exact disposable label.

## Disposable collection lifecycle

One disposable collection exercised the corresponding account-level lifecycle:

1. `CreateLabel` with discriminator `3` created a uniquely named collection containing one
   existing notebook ID; the direct response and `GetLabels` read-back agreed on its canonical ID.
2. `MutateLabel` changed the name and emoji; read-back matched both.
3. A one-member mutation added a second notebook; read-back contained both IDs.
4. A second mutation removed the first; read-back retained only the second.
5. `DeleteLabels` with discriminator `3` removed the exact collection.

All created labels and collections were deleted. Grouping operations did not modify or delete the
referenced sources or notebooks.

## Live mutation matrix

Every write used disposable names and IDs.

| Test | Result |
|---|---|
| list source labels | gRPC success |
| create, rename, and delete source label | success plus `GetLabels` read-back |
| add and remove source membership | success plus membership read-back |
| list notebook collections | gRPC success |
| create, rename, and delete collection | success plus `GetLabels` read-back |
| add and remove notebook membership | success plus membership read-back |
| copy empty notebook | success, distinct project UUID |
| copy notebook containing one URL source | success, distinct source UUID |
| copy notebook containing 50 sources and five Studio artifacts | success on first read-back; all 55 child UUIDs distinct |
| mutate/delete copied children | source rename/delete, report rename/delete, and note create/update/delete verified |

## Evidence precedence and implementation boundary

An older comparison manifest recorded unsuccessful guessed `CreateLabel` bodies and
`UNIMPLEMENTED` for candidate `MutateLabel`/`DeleteLabels` calls. The 2026-08-29 run used the later
wire shapes and current owned resources, then completed create/read-back, property mutation,
membership mutation, and deletion. That later valid-resource result supersedes the earlier rejected
candidate shapes.

The admitted boundary is `GetLabels`, manual and automatic `CreateLabel`, property and one-member
`MutateLabel`, and `DeleteLabels` for source labels and notebook collections. The current bundle
pins automatic creation to the `CreateLabel` request union at field `#5`, whose nested optional bool
at field `#1` distinguishes unlabeled-only (`false`) from destructive regenerate-all (`true`).
Disposable Android-bearer probes verified both modes and cleanup. The selected Android labels
adapter therefore performs `labels.generate` natively and does not retain a Web generation
callable.

## Public adapter qualification

Explicit `backend="android"` selects all eleven Android namespace adapters, including
`AndroidCollectionsAPI`. Its permanent authenticated E2E gate
passed twice independently. Each run created a fresh disposable Web-backed notebook and an
Android-backed collection, then exercised `list`, `get_or_none`, `get`, `notebooks`, `create`,
`rename`, `add_notebooks`, `remove_notebooks`, and `delete`.

Both runs verified membership expansion, deleted the exact collection and notebook
IDs in `finally`, and left no created resource behind. The selected lifecycle loaded the isolated
profile's credential only during async open and kept gRPC channel construction lazy until the first
collection call. No browser was opened. The 82-test Collections SDK/application/CLI/VCR slice also
passed twice. Collections has no MCP or REST route, so no additional frontend envelope was
required.

Manual source-label Android CRUD, notebook operations, source operations, and note CRUD/mind-map
operations are now selected publicly. Source refresh, upload-only Drive-file import, and note-backed
mind-map generation no longer require Web collaborators. Follow-up two-account probing established
that notebook recent-removal succeeds natively for genuinely shared notebooks; the `INTERNAL`
result below is specific to owned notebooks and is folded into an already-absent no-op. It is no
longer a Web compatibility seam.

## Native source refresh and Drive-file import

The current bundle closes `RefreshSourceRequest` as `SourceId #2` plus `RequestContext #3`; the
response wraps the refreshed `Source #1`. Earlier rejected probes used URL sources or incomplete
candidate conditions. A later bounded run created a nonempty native Google Doc by Drive conversion,
added it to a disposable notebook, and observed `CheckSourceFreshness = false`. The exact
`RefreshSource` request then succeeded through the Android bearer. The public method retains its
documented `None` return while the response is decoded only as protocol evidence. The converted
Drive document was deleted by exact ID with HTTP 204, followed by deletion of the exact notebook.

Upload-only Drive files use a different local composition. The Android bearer already carries the
Drive scope, so the upload pipeline now reads Drive v3 metadata and `alt=media` directly. It
preserves link resource keys, rejects native Google Docs/Slides/Sheets with `add_drive` guidance,
admits only the public upload extension set, enforces a 200 MiB header and running byte cap, and
streams into a mode-0700 temporary directory containing a mode-0600 file with the sanitized
original name. Redirects are disabled and the bearer is sent only to the fixed
`www.googleapis.com` origin. The temporary directory is removed on success, error, or cancellation.

A live run created a tiny Drive text file, downloaded it through that Android path, uploaded it to
NotebookLM, waited for READY, and read back the original title. The external Drive file deletion
returned HTTP 204 and the exact disposable notebook was deleted. This replaces the earlier
cookie-authenticated Web download collaborator; the subsequent NotebookLM upload remains the
native Android tentative-registration and Scotty data-plane transaction.

## Notebook metadata lifecycle

The 2026-08-28 probe created one uniquely titled notebook, verified it in
`ListRecentlyViewedProjects`, exercised candidate mutations, and deleted it in `finally`.

### Authentication control

Before mutations, the short-lived bearer completed `GetOrCreateAccount` with gRPC status `0`; the
response contained 69 serialized bytes. This proves backend acceptance rather than token issuance
alone.

### Disposable-notebook sequence

| Operation | Request evidence | Result |
|---|---|---|
| Set emoji | `MutateProjectRequest.project_id #1`; repeated mutation `#2`; `change_property #4`; repository-local `new_emoji #3` | bare `Project` response and `GetProject` read-back matched |
| Clear emoji | same shape, explicitly encoding a zero-length string at `new_emoji #3` | response and read-back returned the empty string |
| Set title and emoji together | `new_title #2` and `new_emoji #3` in one change-property message | both fields matched in response and read-back |
| Remove from recent | exact `project_id #1`, without fabricated context | gRPC `INTERNAL` (`13`) |
| Final cleanup | `DeleteProjects` with the exact disposable ID | success |

The emoji field is a repository-local wire overlay. Successful round trips prove its tag and
semantics but not an unrecovered Google descriptor name.

### Promotion boundary

Emoji set, explicit clear, and combined title/emoji update are native Android operations.
At this validation checkpoint, `RemoveRecentlyViewedProject` remained unusable
for the owned-notebook resource used here: reproducing status `13` on a fresh
notebook ruled out the earlier copied-notebook setup, so the selected
`AndroidNotebooksAPI` delegated `remove_from_recent` to an injected Web callable.
Later two-account probing showed that the exact mobile route succeeds for a
genuinely shared notebook; the current adapter uses it natively and folds the
owned-notebook `INTERNAL` result into an already-absent no-op. See
[`web-compat-seam-closure.md`](web-compat-seam-closure.md#notebooksremove_from_recent--wrong-resource).

## Note-backed mind-map lifecycle

### Result

Two earlier independent runs generated a note-backed mind map through Web and read the same
persisted row through Android `GetNotes`. IDs matched in both runs. Each Android row carried:

- `ProjectNote.metadata.type = USER_WRITTEN (1)`;
- `ProjectNote.metadata.note_prompt_type = NOTE_PROMPT_TYPE_UNSPECIFIED (0)`; and
- JSON content with a top-level `children` key.

Prompt metadata alone therefore cannot classify Web-generated maps. The evidence-safe classifier
matches the public Web contract:

```text
prompt type is MIND_MAP
OR parsed content is a JSON object containing top-level "children" or "nodes"
```

Ordinary-note listing must negate the same predicate so a Web-generated map is never exposed as a
text note.

### Native generation and persistence

The APK supplies the exact `ActOnSources` request/response FQNs. Its recovered request includes
sources `#1` and request context `#8`; the current product constructor closes mind-map action field
`#6` as action string `#1`, repeated context key/value rows `#2`, and language `#3`. The Android
adapter sends action `interactive_mindmap`, key `[CONTEXT]`, caller instructions, and language.

A bounded live run used one disposable text source. `ActOnSources` returned a nonempty JSON tree
with root `NotebookLM Features`; Android `CreateNote` persisted the exact JSON in the same lifecycle
epoch and returned a canonical note ID. The exact notebook was then deleted. Public generation no
longer calls the Web generator, while the historical cross-backend rows above remain useful evidence
for the classifier and read/delete semantics.

### Deletion lifecycle

One ordinary note and one generated map existed as siblings. A single non-replayed Android
`DeleteNotes` request targeting the exact map ID removed only the map. Bounded `GetNotes` read-back
observed it absent while retaining the ordinary note. Repeating the public delete against the
already absent ID also returned success because the adapter preflight avoided a redundant
destructive request.

The qualified deletion workflow is:

1. read `GetNotes` and classify the target as a map;
2. succeed without mutation if that map is absent, including when the ID belongs to an ordinary
   note;
3. send `DeleteNotes` once with replay disabled;
4. treat a concurrent status-5 absence as idempotent success; and
5. poll bounded safe reads until the classified map disappears, failing loudly if it remains.

### Public raw compatibility boundary

The legacy `NotesAPI.list_mind_maps` contract returns raw Web rows. Android proves only the map ID
and content needed by existing leading-slot consumers, so the honest compatibility row is exactly
`[id, content]`. It does not synthesize Web-only status, metadata, title, or creation time. A private
typed aggregate may additionally use the exact Android name and parsed tree, but leaves
`created_at=None` because the recovered timestamp is specifically a last-edit time.

### Ordinary-note soft-delete boundary (2026-08-29)

Two consecutive complete-manifest reruns created and updated an ordinary note through Android,
then deleted it through the non-replayed `DeleteNotes` path. Android bounded read-back reported the
ID absent, and both Android and Web ordinary-note lists excluded it. Web exact-ID `get_or_none`,
however, exposed the persisted soft-delete row as `Note(id=<same id>, title="", content="")`,
matching Web's raw `[id, None, 2]` tombstone behavior. Android's recovered `NoteOrStatus` note arm
did not expose a `ProjectNote` capable of reproducing that exact result.

This is not a public-contract blocker. `get_or_none` promises `None` for genuine absence, while the
Web tombstone is a storage-specific leak rather than a documented result. The selected Android
adapter correctly treats the status-only or absent projection as deleted and does not fabricate a
Web tombstone `Note`.

## Reproducer usage

Run the organization reproducer from its repository without printing names or resource IDs:

```bash
cd ../gemini-notebook-mobile

uv run scripts/reproduce_mobile_organization.py list-labels \
  --profile PROFILE --redact

uv run scripts/reproduce_mobile_organization.py list-collections \
  --profile PROFILE --redact
```

Create and mutate source labels:

```bash
uv run scripts/reproduce_mobile_organization.py create-label "Topic" \
  --emoji "🧪" --profile PROFILE
uv run scripts/reproduce_mobile_organization.py set-label LABEL_UUID \
  --name "Renamed topic" --profile PROFILE
uv run scripts/reproduce_mobile_organization.py add-sources LABEL_UUID \
  SOURCE_UUID [SOURCE_UUID ...] --profile PROFILE
uv run scripts/reproduce_mobile_organization.py remove-sources LABEL_UUID \
  SOURCE_UUID [SOURCE_UUID ...] --profile PROFILE
uv run scripts/reproduce_mobile_organization.py delete-label LABEL_UUID --profile PROFILE
```

Create and mutate collections:

```bash
uv run scripts/reproduce_mobile_organization.py create-collection "Research" --profile PROFILE
uv run scripts/reproduce_mobile_organization.py add-notebooks COLLECTION_UUID \
  NOTEBOOK_UUID [NOTEBOOK_UUID ...] --profile PROFILE
uv run scripts/reproduce_mobile_organization.py remove-notebooks COLLECTION_UUID \
  NOTEBOOK_UUID [NOTEBOOK_UUID ...] --profile PROFILE
uv run scripts/reproduce_mobile_organization.py delete-collection COLLECTION_UUID --profile PROFILE
```

Label commands default to the selected profile's configured multi-source notebook; pass
`--notebook-id` to target another notebook. Collections are account-scoped.

## Capture these calls through HTTP Toolkit

The full emulator and CA-install procedure is in [capture.md](capture.md). Once its Mockttp
recorder listens on `127.0.0.1:8081`, a host-side replay can be captured without opening the app:

```bash
cd ../gemini-notebook-mobile

grpc_proxy=http://127.0.0.1:8081 \
GRPC_DEFAULT_SSL_ROOTS_FILE_PATH=/path/to/httptoolkit/ca.pem \
uv run scripts/reproduce_mobile_organization.py list-collections \
  --profile PROFILE --redact
```

Filter captured paths for `GetLabels`, `CreateLabel`, `MutateLabel`, `DeleteLabels`, or
`CopyProject`. Redacted CLI output does not redact protobuf bodies in the proxy recorder; captures
contain resource IDs and user-chosen names. Keep raw captures outside the repository unless they
have been structurally decoded and scrubbed.

## Public product confirmation

Google's [Create a notebook in Gemini Notebook](https://support.google.com/gemininotebook/answer/16206563?hl=en)
help page says private copies include sources and Studio content but not notes or chat history. The
[source help page](https://support.google.com/notebooklm/answer/16215270?hl=en) documents automatic
and manual source labels. Neither page proves Android UI support; the live Android-bearer calls are
the backend evidence.

## Cleanup ledger

- Every disposable label and collection was deleted by exact ID.
- Copy probes deleted the empty/one-source disposable originals and every created copy; the
  rich-copy audit recovered its copy by unique title prefix, deleted it, and verified no
  audit-prefixed copy remained.
- Organization E2E runs deleted their exact collection and notebook IDs in `finally`; neither run
  left a resource behind.
- The notebook metadata probe deleted its exact notebook through `DeleteProjects`.
- Both map-classification runs and both ordinary-note tombstone reruns deleted their uniquely
  prefixed disposable notebooks; final prefix scans found no leaked resource.
