# Web-parity gap probes over mobile gRPC

**Status:** Mobile routing checked for every `notebooklm-py` method absent from the inspected APK

**Live validation:** 2026-08-27

This run answers a narrower question than the APK inventory: when the Android binary does not
compile a method, does `notebooklm-pa.googleapis.com` still route that method for an Android OAuth
bearer? The answer is yes for all 15 current gaps, but routing and usable semantics are not the
same claim.

No credential, notebook/source/artifact ID, title, source URL, or source content was printed. A
source- and artifact-rich owned notebook was copied first. All valid mutations and deletes below
targeted only that disposable copy, and the copy was deleted at the end.

## Test target and copy fidelity

The new web client operation was used from the isolated branch
`feat/mobile-copy-endpoint-audit`:

```python
copied = await client.notebooks.copy(source_notebook_id, disposable_title)
```

It sends `te3DCe → CopyProject` with:

```text
#1 RequestContext
#2 source project UUID
#3 destination title
```

The selected original had 50 sources and 5 Studio artifacts:

```text
sources: 44 web pages, 4 PDFs, 1 CSV, 1 Markdown
artifacts: 1 audio, 1 report, 1 data table, 1 infographic, 1 slide deck
```

The copy returned all 50 sources and all 5 artifacts on its first read-back. The intersection
between original and copied source IDs was empty, and the intersection between original and copied
artifact IDs was empty. This upgrades the earlier one-source probe: `CopyProject` copies both
sources and Studio artifacts, allocating new child identities.

## The 15 APK-absent web methods

| family | methods absent from APK | mobile bearer/gRPC result |
|---|---|---|
| labels/collections | `CreateLabel`, `MutateLabel`, `DeleteLabels` | valid-resource success; full CRUD and membership read-back |
| async Research | `DiscoverSourcesManifold`, `DiscoverSourcesAsync`, `ListDiscoverSourcesJob`, `CancelDiscoverSourcesJob` | valid lifecycle success |
| notebook copy | `CopyProject` | valid 50-source/5-artifact copy success |
| source maintenance | `MutateSource`, `CheckSourceFreshness` | valid copied-source success |
| report suggestions | `GenerateReportSuggestions` | valid copied-notebook success; four rows |
| source refresh | `RefreshSource` | route exists, but valid copied URL source returned `INVALID_ARGUMENT` |
| side-effect routes | `GenerateArtifact`, `ExportToDrive`, `ShareAudio` | `NOT_FOUND` for nonexistent UUIDs; route proven without creating state |

Every one returned something other than `UNIMPLEMENTED`, so all 15 method paths are present on the
mobile host. Only the rows marked valid-resource success establish the operation's semantics.

## Newly recovered successful request shapes

The request context was optional for the successful calls below.

### MutateSource

```text
request:
  #2 SourceId { #1 source UUID }
  #3 repeated SourceMutation {
       #1 ChangeTitle { #1 new title }
     }
  #4 RequestContext (optional in this replay)
```

The copied source's title changed and the web read path returned the new title.

### CheckSourceFreshness

```text
request:
  #2 SourceId { #1 source UUID }
  #3 RequestContext (optional in this replay)
```

The nested field-2 ID is load-bearing. A raw string at field 2, or the same nested/string value at
field 1, returned `INVALID_ARGUMENT`. The valid nested request returned gRPC success; its empty
response is the same healthy shape the web decoder interprets as fresh.

### GenerateReportSuggestions

```text
request:
  #1 RequestContext (optional in this replay)
  #2 project UUID
  #3 repeated SourceId (optional filter; omitted here)

response:
  #1 repeated suggestion
```

The copied notebook returned four suggestion rows.

## RefreshSource: route present, operation rejected

The current web bundle builds the same semantic request as freshness checking:

```text
#2 SourceId { #1 source UUID }
#3 RequestContext
```

The direct mobile test tried all of these context variants with the correct nested source ID:

1. minimal web context (`clientType = 2`);
2. minimal Android context (`clientType = 3`);
3. an encoded empty context message;
4. the full Android version/provenance context used by the upload reproducer.

All four returned `INVALID_ARGUMENT`. Field-2 raw string and field-1 nested/raw alternatives also
returned `INVALID_ARGUMENT`. This is not an unknown route: `UNIMPLEMENTED` was never returned.

As a control, web `FLmJqe` refreshed three copied URL sources successfully, including sources whose
freshness check was already true. The evidence therefore supports this precise statement:

> `RefreshSource` is routed by the mobile gRPC host, but a request that is valid through the web
> transport was rejected through the mobile bearer endpoint in this account/build cohort.

It does not support claiming full mobile refresh parity yet. A future probe should use a copied
Google Drive source and an actual Android capture if one becomes UI-reachable.

## Safe route-only probes for high-side-effect methods

These calls used canonical but nonexistent UUIDs. A `NOT_FOUND` response proves the request reached
the named handler while ensuring no valid artifact was generated, exported, or shared.

```text
GenerateArtifact:
  #1 RequestContext
  #2 nonexistent artifact UUID
  result: NOT_FOUND

ExportToDrive:
  #1 RequestContext
  #2 nonexistent artifact UUID
  #4 disposable title
  #5 destination = 1 (Docs)
  result: NOT_FOUND; no Drive file created

labs.language.tailwind.sharing.LabsTailwindSharingService/ShareAudio:
  #1 share option
  #2 copied project UUID
  #3 nonexistent artifact UUID
  result: NOT_FOUND; no share state changed
```

These are route checks, not semantic success tests. A valid-ID export would create a Drive file and
a valid-ID share could change public state, so neither was justified merely to prove dispatch.

## APK-present destructive validation on the copy

The same run also filled several “compiled but not UI-exercised” gaps:

| operation | result |
|---|---|
| `CreateNote` | created a disposable note; ID and row read back |
| `MutateNote` | content and title changed; both read back |
| `DeleteNotes` | gRPC success; immediate lookup briefly saw the row, next list excluded it |
| `UpdateArtifact` | copied report title changed and read back |
| `DeleteArtifact` | copied report disappeared; artifact count changed 5 → 4 |
| `DeleteSources` | renamed copied URL source disappeared; source count changed 50 → 49 |

`RemoveRecentlyViewedProject` was also attempted against the owned copy with a valid Android
context. It returned `INTERNAL`, so it is not marked live-successful in the endpoint matrix. The
copy was finally recovered by its unique disposable title prefix, deleted through the web
`DeleteProjects` RPC, and verified absent; no audit-prefixed copy remained.

## What this changes

- APK method strings are a **client inventory**, not a backend capability list.
- The current mobile host routes every method in the 48-method web library surface.
- Eleven of the 15 APK gaps have valid-resource semantic proof.
- Three more have safe route proof only.
- `RefreshSource` remains the one valid-resource mismatch and should stay explicitly qualified.

The complete 48-row cross-reference is maintained in [endpoints.md](endpoints.md).
