# Closing the Android backend's Web compatibility seams

**Status:** live-validated

**Last verified:** 2026-08-31

**Scope:** the four operations an Android-selected `NotebookLMClient` still
routed through the Web namespace after [#2269], why three of them were not
actually gaps, and what remains.

## Summary

| Operation | Before | After | Root cause |
|---|---|---|---|
| `sharing.set_view_level` | Web seam | **native** | Probed the wrong RPC |
| `notebooks.remove_from_recent` | Web seam | **native** | Probed an owned notebook |
| `sources.add_file` (`.csv`) | Web seam | **native (via Drive)** | Mobile upload frontend has no CSV parser |
| `sources.add_file` (`.docx`, `.pptx`) | Web seam | **native (via Drive)** | Mobile upload frontend parses no OOXML office container; the backend does |

`tests/unit/test_backend_selection.py::test_android_preference_promotes_every_namespace`
asserts the complete inventory of remaining Web bindings. It is now **empty**:
an Android-selected namespace graph holds no Web operation collaborator, and
typed Android operations do not fall back to batchexecute. The composition root
now constructs only `AndroidRuntime` for normal Android use. The deprecated
`client.rpc_call(...)` wrapper remains Web-specific and pre-registers an inert
lifecycle proxy, but constructs its no-keepalive Web compatibility sidecar only
on first use; this report is about the installed typed namespace graph, not that
explicit v0.x compatibility exception.

## `sharing.set_view_level` — wrong service

[#2269] recorded "the recovered mobile mutation returns `PERMISSION_DENIED` for
owned notebooks" and kept the Web collaborator. That probed
`LabsTailwindSharingService/ShareProject`, whose `PublicDocumentSettings`
(`is_publicly_readable` / `is_discoverable`) is the *public-access* toggle — a
different axis from `ShareViewLevel` (`FULL_NOTEBOOK` / `CHAT_ONLY`).

The view level is not a sharing mutation on either front door. `_web/sharing.py`
posts `RPCMethod.RENAME_NOTEBOOK` — the live `MutateProject` — with:

```python
[notebook_id, [[None, None, None, None, None, None, None, None, [[level.value]]]]]
```

Index 8 is proto tag **9** under the positional mapping already proven twice in
this tree:

| Web params index | Proto tag | Where |
|---|---|---|
| `[_, _, _, [_, title, emoji]]` → 3 | `ProjectMutation.change_property = 4` | `orchestration/v1/notebooks.proto` |
| inner 1 / 2 | `new_title = 2`, emoji `= 3` | `_web/notebooks.py` |
| `[_ ×7, chat_settings]` → 7 | `WireProjectMutation.advanced_settings = 8` | live read-back, 2026-08-29 |
| `[_ ×8, [[level]]]` → 8 | `WireProjectMutation.change_view_level = 9` | **this document** |

**Read-back oracle.** Neither `GetProjectDetails` nor `GET_SHARE_STATUS` reports
the view level, so it was established out of band: setting it through the Web
front door and reading the raw notebook payload moves index 8 between `[false]`
and `[true]`. The Android `GetProject` response carries the same state at tag 9
as `{level:#1}` — hex `0800` for `FULL_NOTEBOOK`, `0801` for `CHAT_ONLY`.

`WireProjectViewLevel.level` is declared `optional` for the same reason
`new_emoji` is: `FULL_NOTEBOOK` is wire value 0, and a plain proto3 scalar would
drop it from the mutation entirely. The server always emits the tag, so presence
— not the default — is the contract.

**Verified.** Writing both levels natively and reading them back through both the
Android `GetProject` projection and the Web raw payload agrees in both
directions, including the return trip to `FULL_NOTEBOOK` that the `optional`
declaration exists to make expressible.

## `notebooks.remove_from_recent` — wrong resource

[#2269] recorded `INTERNAL` and kept the Web collaborator. The route is not
broken; it refuses *owned* projects.

`ListRecentlyViewedProjects` returns nothing for this account unless
`include_own_projects` is set — the recently-viewed list holds projects **shared
with** the caller, and owned projects surface only under that separate flag. The
earlier probe used an owned notebook, which the mutation legitimately rejects.

Sharing a notebook from one account to a second and removing it there through
the Android bearer succeeds, with and without the `RequestContext` at tag 2, and
the project leaves `ListRecentlyViewedProjects(include_own_projects=False)`.

The Web control on an **owned** notebook returns success and leaves the project
in place — a vacuous no-op. The adapter therefore folds `INTERNAL` into the same
no-op rather than surfacing a backend-visible parity break: the postcondition
("not in the recently-viewed list") already holds in exactly that case. Every
other status propagates.

## `sources.add_file` — the mobile upload frontend's allowlist

The two front doors use different Scotty frontends:

| | Endpoint |
|---|---|
| Web | `https://notebooklm.google.com/upload/_/` |
| Android | `https://notebooklm-pa.googleapis.com/upload/upload/{project_id}` |

Everything else about the two transactions matches: both register by filename
only (`TentativeSourceMetadata` has just `name = 1`; the Web params are
`[[[filename]], notebook_id, <template block>]`), both resolve the content type
through the same helper, and neither commits afterwards. The APK confirms the
Android shape — `SourceService.uploadFile` calls `ScottyUploader.startUpload`,
then `pollForSourceUploadComplete` and `getProject`, with no `AddSources` hop.

The difference is which content types the frontend's ingester parses.

### Source type does not survive any Android upload route

Comparing on **both** the ingested text and the decoded `SourceType`:

| Route | `kind` | text vs Web |
|---|---|---|
| Web `add_file` (`.csv`) | `CSV` (16) | — |
| Android Scotty as `text/plain` | `PASTED_TEXT` (4) | different |
| Android `AddSources` + `CONTENT_TYPE_CSV` | `PASTED_TEXT` (4) | different |
| Android Drive-staged | code 14 | **identical** |
| Web `add_file` (`.docx` / `.pptx`) | `DOCX` (11) / `POWERPOINT` (6) | — |
| Android Drive-staged (`.docx` / `.pptx`) | code 14 | **identical** |
| Web *and* Android, `.pdf` | `PDF` (3) | identical |

Two things this settles.

**Sending CSV as `text/plain` is not "native CSV".** It reaches READY, but the
source is `PASTED_TEXT` carrying the raw delimited rows, not a CSV source. Any
caller branching on `source.kind` sees a different object. That rewrite has been
removed; `.csv` now takes the Drive route with the other two, where the ingested
text matches Web byte for byte.

**No Android route reproduces `CSV`, `DOCX`, or `POWERPOINT`.** The Drive import
types a non-Google-native file as **14**, which the recovered mobile enum names
`SOURCE_CONTENT_TYPE_DRIVE`. A genuine Google Doc imported the same way does keep
its type (`GOOGLE_DOCS`, code 1), so Drive is not flattening everything — it
records "a Drive file" for formats it does not convert. The type is assigned by
the ingestion entry point, and the mobile upload entry point has no parser for
these three formats to reach.

So Android `add_file` delivers **content parity** for every supported extension
and **type parity** for all but `.csv`, `.docx` and `.pptx`, which report the
Drive type instead. That is a real, bounded divergence, recorded here rather
than hidden.

> **Follow-up decode fix.** At the August 31 validation point,
> `_SOURCE_TYPE_CODE_MAP` still mislabeled `14` as `GOOGLE_SPREADSHEET` and had
> no entry for `7`. The current decoder matches the recovered enum: `14`
> defaults to `GOOGLE_DRIVE`, while recognized MIME evidence refines a native
> Sheet to code `7` (`GOOGLE_SPREADSHEET`) and a Drive PDF to code `3` (`PDF`).
> See `_disambiguate_type_code` in `src/notebooklm/_types/sources.py`.

### `.docx` / `.pptx` — native by way of Drive

Four findings, none of which yields a direct upload:

1. **The mobile frontend refuses it** under every candidate content type —
   `application/vnd.openxmlformats-…wordprocessingml.document`,
   `application/msword`, `application/octet-stream`. Each transfers, then errors.
2. **The app never uploads one.** `ContentMimeType`
   (`content_picker/content_mimetype.dart`) is the app's complete picker set:
   `unsupported`, `m4a`, `mp3`, `wav`, `wma`, `pdf`. The mobile upload frontend
   was never built to parse Word.
3. **The mobile backend *can* parse Word.** The same file imported through
   `AddSources` / `GoogleDriveContent` reaches `SOURCE_STATUS_COMPLETE` with
   correct extracted text, and `SourceContentType` reserves `WORD = 11`. The gap
   is the upload frontend's allowlist, not a missing parser.
4. **The Web frontend cannot be borrowed with an Android bearer.** It accepts the
   bearer for the upload `start` and issues a session URL, then fails closed on
   finalize:

   ```text
   APPLICATION_ERROR;google.internal.labs.tailwind.orchestration.v1/
   LabsTailwindOrchestrationService.AddSourcesAsync;
   com.google.security.context.validation.CredsPermissionException:
   Rejected by impersonation policy for /…AddSourcesAsync
   ```

   `AddSourcesAsync` appears in no mobile binary string, and this refusal is a
   server-side credential-class boundary on the **Web** front door, not a header
   that can be added. *Correction (2026-09-01, #2283):* the native gRPC route
   `/LabsTailwindOrchestrationService/AddSourcesAsync` accepts the same Android
   bearer and commits — the block is the Web frontend refusing to impersonate a
   mobile credential, not a missing mobile route. See
   [copy-append-suggestion-evidence.md](copy-append-suggestion-evidence.md#addsourcesasync).

**No content type makes the native transaction work.** The declared type was
swept across the OOXML types, `application/zip`, `application/x-zip-compressed`,
`application/epub+zip`, `application/vnd.google-apps.document`,
`application/vnd.ms-word.document.macroEnabled.12` and
`application/vnd.oasis.opendocument.text` — all error. The one that does not,
`text/plain`, reports **READY while ingesting the raw ZIP container as text**:
3,325 characters beginning `PK\x03\x04...`, with none of the document's words
present. Renaming the file changes nothing, so the ingester dispatches on the
declared content type, not the extension. A `READY` status is not evidence of a
correct ingest, and CSV's `text/plain` rewrite does not generalise.

`application/epub+zip` ingesting `.epub` correctly is the useful contrast: the
frontend's allowlist is **not** the app's picker set (the app has no EPUB picker
entry), and it does handle a ZIP container — just not an OOXML one.

Finding 3 is the route. `DriveStagingTransfer` (`_android/drive_staging.py`)
stages such files in the caller's own Drive with the `auth/drive` scope the
Android identity already holds, imports it with `GoogleDriveContent`, and
deletes the staged copy. Deleting it is safe: the import materializes the
content, and a live probe confirmed the source stays `READY` with its text
intact and `drive_status: ACTIVE` afterwards.

Three consequences are documented on `add_file_via_drive_staging` and worth
repeating here:

* the path always waits for readiness, whatever `wait` asked for — the staged
  copy cannot be removed until the import has materialized the content;
* the resulting source is Drive-backed, so `drive_status` describes a staged
  copy that no longer exists rather than a live document;
* `on_progress` is not reported — staging is one multipart request, not a
  chunked transfer.

Cleanup runs on the failure path too, and a failed cleanup is logged rather than
raised: an orphaned staging file is untidy, not a failed add.

[#2269]: https://github.com/teng-lin/notebooklm-py/pull/2269
