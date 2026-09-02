# RPC & UI Reference

**Status:** Active
**Last Updated:** 2026-09-02
**Source of Truth:** `src/notebooklm/rpc/types.py` for method IDs; payload builders under `src/notebooklm/_web/` and golden tests under `tests/unit/`
**Purpose:** Reference for the Web backend's batchexecute methods, UI selectors, and payload structures

This reference is deliberately Web-specific: `RPCMethod` and
`client.rpc_call(...)` describe the batchexecute transport even when an
explicitly selected Android client installs native implementations for all 11
typed namespaces. For Android gRPC method contracts, start with the
[Android evidence index](android/README.md) and
[`android/endpoints.md`](android/endpoints.md).

> **Note:** Payload structures are extracted from the implementation builders in
> `src/notebooklm/_web/` and pinned by golden unit tests. Each payload includes a
> reference to its owning source file. The CREATE_ARTIFACT payloads below were
> re-verified against the live builders in `_web/params/artifacts.py` on
> 2026-06-11 (AUDIO, VIDEO_EXPLAINER, VIDEO_BRIEF, VIDEO_CINEMATIC,
> STUDY_GUIDE, BRIEFING_DOC, BLOG_POST, MIND_MAP, QUIZ, FLASHCARDS,
> INFOGRAPHIC, SLIDE_DECK, DATA_TABLE). Read-only notebook/source/artifact/chat/
> sharing/settings RPCs were live-captured again on 2026-06-15. Home, Sources,
> chat, and Studio selectors were rechecked the same day against a live Chrome
> session on a probe notebook.

---

## Quick Reference

### RPC Method Status

| RPC ID | Method | Purpose | Implementation |
|--------|--------|---------|----------------|
| `wXbhsf` | LIST_NOTEBOOKS | List all notebooks | `_web/notebooks.py` |
| `CCqFvf` | CREATE_NOTEBOOK | Create new notebook | `_web/notebooks.py` |
| `te3DCe` | COPY_NOTEBOOK | Copy notebook sources and Studio artifacts | `_web/notebooks.py` |
| `rLM1Ne` | GET_NOTEBOOK | Get notebook details + sources | `_web/notebooks.py` |
| `s0tc2d` | RENAME_NOTEBOOK | Rename, chat config, share access | `_web/notebooks.py`, `_web/chat.py` |
| `WWINqb` | DELETE_NOTEBOOK | Delete a notebook | `_web/notebooks.py` |
| `izAoDd` | ADD_SOURCE | Add URL/text/YouTube/Drive source | `_web/sources/add.py` via `_web/sources/__init__.py` |
| `o4cbdc` | ADD_SOURCE_FILE | Register uploaded file (PDF, DOCX, EPUB, etc.) | `_web/sources/upload.py`, `_web/params/sources.py` |
| `tGMBJ` | DELETE_SOURCE | Delete a source | `_web/sources/__init__.py` |
| `b7Wfje` | UPDATE_SOURCE | Rename source | `_web/sources/__init__.py` |
| `tr032e` | GET_SOURCE_GUIDE | Get source summary | `_web/sources/content.py` |
| `hizoJc` | GET_SOURCE | Get clean fulltext content of a source | `_web/sources/content.py` |
| `agX4Bc` | CREATE_LABEL | AI-generate label groupings and create manual labels | `_web/labels.py` |
| `I3xc3c` | LIST_LABELS | List source labels for a notebook | `_web/labels.py` |
| `le8sX` | UPDATE_LABEL | Rename label, set emoji, add/remove sources | `_web/labels.py`, `_web/params/labels.py` |
| `GyzE7e` | DELETE_LABEL | Delete one or more labels (batch) | `_web/labels.py` |
| `R7cb6c` | CREATE_ARTIFACT | Unified artifact generation | `_artifacts.py`, `_web/artifact/generation.py` |
| `gArtLc` | LIST_ARTIFACTS | List artifacts in a notebook | `_web/artifacts.py` |
| `V5N4be` | DELETE_ARTIFACT | Delete artifact | `_web/artifacts.py` |
| `KmcKPe` | REVISE_SLIDE | Revise an individual slide via prompt | `_web/artifacts.py` |
| `Rytqqe` | RETRY_ARTIFACT | Retry a failed Studio artifact in place | `_web/artifacts.py` |
| `hPTbtc` | GET_LAST_CONVERSATION_ID | Get most recent conversation ID | `_web/chat.py` |
| `khqZz` | GET_CONVERSATION_TURNS | Get Q&A turns for a conversation | `_web/chat.py` |
| `oXwmh` | GET_CHAT_SESSION_STATUS | Read idle/generating state and token for a chat session | `_web/chat.py` |
| `XgrPMd` | CANCEL_GENERATION | Stop active generation for a chat session | `_web/chat.py` |
| `J7Gthc` | DELETE_CONVERSATION | Delete a conversation (web UI's "Delete history") | `_web/chat.py` |
| `otmP3b` | SUGGEST_PROMPTS | Get AI-suggested prompts for a notebook | `_web/notebooks.py` |
| `CYK0Xb` | CREATE_NOTE | Create a note (placeholder) | `_web/notes.py` |
| `cYAfTb` | UPDATE_NOTE | Update note content/title | `_web/notes.py` |
| `AH0mwd` | DELETE_NOTE | Delete a note | `_web/notes.py` |
| `cFji9` | GET_NOTES_AND_MIND_MAPS | List notes and mind maps | `_web/notes.py` |
| `yyryJe` | GENERATE_MIND_MAP | Mind map generation | `_web/artifact/generation.py` |
| `VfAZjd` | SUMMARIZE | Get notebook summary | `_web/notebooks.py` |
| `FLmJqe` | REFRESH_SOURCE | Refresh URL/Drive source | `_web/sources/__init__.py` |
| `yR9Yof` | CHECK_SOURCE_FRESHNESS | Check if source needs refresh | `_web/sources/__init__.py` |
| `Es3dTe` | DISCOVER_SOURCES | Synchronous source discovery | `_web/research.py` |
| `Ljjv0c` | START_FAST_RESEARCH | Start fast research | `_web/research.py` |
| `QA9ei` | START_DEEP_RESEARCH | Start deep research | `_web/research.py` |
| `e3bVqc` | POLL_RESEARCH | Poll research status | `_web/research.py` |
| `LBwxtb` | IMPORT_RESEARCH | Import research results | `_web/research.py` |
| `Zbrupe` | CANCEL_RESEARCH | Cancel in-flight research run | `_web/research.py` |
| `rc3d8d` | RENAME_ARTIFACT | Rename artifact | `_web/artifacts.py` |
| `Krh3pd` | EXPORT_ARTIFACT | Export to Docs/Sheets | `_web/artifacts.py` |
| `RGP97b` | SHARE_ARTIFACT | Legacy notebook/artifact share-link toggle | `_web/sharing.py` |
| `QDyure` | SHARE_NOTEBOOK | Set notebook visibility (restricted/public) | `_web/sharing.py` |
| `JFMDGd` | GET_SHARE_STATUS | Get notebook share settings | `_web/sharing.py` |
| `ciyUvf` | GET_SUGGESTED_REPORTS | Get AI-suggested report formats | `_web/artifacts.py` |
| `v9rmvd` | GET_INTERACTIVE_HTML | Fetch quiz/flashcard HTML (`[0][9][0]`) / interactive mind-map tree (`[0][9][3]`) | `_web/artifact/downloads.py` |
| `fejl7e` | REMOVE_RECENTLY_VIEWED | Remove notebook from recent list | `_web/notebooks.py` |
| `ZwVcOc` | GET_USER_SETTINGS | Get user settings including output language | `_web/settings.py` |
| `hT54vc` | SET_USER_SETTINGS | Set user settings (e.g., output language) | `_web/settings.py` |
| `X1snv` | ADD_SOURCES_ASYNC | Queue URL sources without waiting for ingest (batch; per-source acks) | `_web/sources/transfers.py` |
| `QsNTEd` | APPEND_SOURCE | Append a plain-text block to an existing source in place | `_web/sources/transfers.py` |
| `R27wvc` | COPY_SOURCES | Copy sources into another notebook (original → copy mapping) | `_web/sources/transfers.py` |
| `mVtEUb` | LIST_EXPERT_INTELLIGENCE_CONTENT | List the account's Google Play Books library — every title, with per-row exportability (Expert Intelligence; Android uses its native RPC counterpart) | `_web/sources/play_books.py` |
| `ASU5Oe` | RETRIEVE_RELEVANT_CHUNKS | Search ranked passages across notebook sources, optionally filtered by source id | `_web/sources/search.py` |
| `mKDdke` | COPY_ARTIFACTS | Copy Studio artifacts into another notebook (full new rows inline) | `_web/artifacts.py` |
| `OcvKNc` | SUGGEST_NEXT_STEPS | Grounded follow-up questions (the chat `next_steps` block, standalone) | `_web/notebooks.py` |
| `sqTeoe` | GET_CUSTOMIZATION_CHOICES | Studio "Customize" option tables (account-level) | `_web/artifacts.py` |

### Content Type Codes (ArtifactTypeCode)

| Code | Type | Used By |
|------|------|---------|
| 1 | Audio | Audio Overview |
| 2 | Report | Briefing Doc, Study Guide, Blog Post |
| 3 | Video | Video Overview |
| 4 | Quiz/Flashcards (QUIZ_FLASHCARD alias) | Quiz (variant=2), Flashcards (variant=1) |
| 5 | Mind Map | Backend mind map type; also used when adapting note-backed mind maps |
| 6 | Fantasy Map | Backend fantasy-map artifact |
| 7 | Infographic | Infographic |
| 8 | Slide Deck | Slide Deck |
| 9 | Data Table | Data Table |
| 10 | File | Backend file artifact |

### Source Type Codes (file uploads & sources)

Internal integer codes returned by `GET_NOTEBOOK` / `LIST_SOURCES` and consumed by
`Source.from_api_response()` (mapped to `SourceType` in
`src/notebooklm/_types/sources.py`).

| Code | `SourceType` | Used By |
|------|--------------|---------|
| 1 | `GOOGLE_DOCS` | Google Docs source |
| 2 | `GOOGLE_SLIDES` | Google Slides source |
| 3 | `PDF` | PDF upload |
| 4 | `PASTED_TEXT` | Inline pasted text |
| 5 | `WEB_PAGE` | Web URL source |
| 6 | `POWERPOINT` | PowerPoint upload (`.pptx`) |
| 7 | `GOOGLE_SPREADSHEET` | Native Google Sheets source after MIME disambiguation |
| 8 | `MARKDOWN` | Markdown file |
| 9 | `YOUTUBE` | YouTube URL |
| 10 | `MEDIA` | Audio / video upload |
| 11 | `DOCX` | Word document |
| 12 | `EXCEL` | Recovered-schema member; no reachable producer is known |
| 13 | `IMAGE` | Image upload |
| 14 | `GOOGLE_DRIVE` | Generic Drive source before MIME disambiguation (see note) |
| 15 | `GMAIL` | Recovered-schema member; no reachable producer is known |
| 16 | `CSV` | CSV upload |
| 17 | `EPUB` | EPUB upload (added in v0.4.0) |
| 18 | `GEMINI_CHAT` | Gemini chat source observed through Android `AddSources` |
| 19 | `AI_MODE_CHAT` | Recovered-schema member; no reachable producer is known |
| 20 | `EXPERT_INTELLIGENCE` | Google Play Books source added via `sources.add_play_book` (#2292); carries `ExpertIntelligenceSourceMetadata` at `metadata[18]` |

> Codes outside this map are surfaced as `SourceType.UNKNOWN` and emit `UnknownTypeWarning` on first occurrence so unmapped types don't crash callers.

> **Code `14` is a generic Drive code** (live-captured #1828/#1832): the backend
> returns it for a native Google Sheet and for Drive-hosted binaries such as a
> PDF. Drive sources carry no URL (`metadata[5]/[7]` are null and `metadata[0]`
> holds the Drive metadata block, not a URL — see
> `SourceRow.drive_document_id`), so recognized original-content MIME values at
> `source[7][2]`, with a Drive-only metadata fallback, refine a Sheet to code 7
> (`GOOGLE_SPREADSHEET`) and a PDF to code 3 (`PDF`). An absent or unrecognized
> MIME remains `GOOGLE_DRIVE`; see `_disambiguate_type_code` in
> `src/notebooklm/_types/sources.py`.

### Source Settings Block (`source[3]`)

`Source.settings` (`SourceSettings`) carries **two independent status codes**, and
they answer different questions:

| Index | Proto tag | Field | Decoded as |
|-------|-----------|-------|------------|
| 1 | 2 | `status` | `SourceStatus` — NotebookLM's own ingestion pipeline (`SourceRow.status`) |
| 3 | 4 | `userDriveSourceStatus` | `DriveSourceStatus` — Drive-side health, Drive-backed rows only (`SourceRow.drive_status`) |

Shapes observed across 409 live source rows (2026-08-07 audit): `[null, 2]` ×402,
`[null, 2, null, 3]` ×4 (all Drive-backed, all `ACTIVE`), and
`[null, 2, [null,null,null,[]]]` ×3.

### Additional Source Metadata (`source[5:8]`, `source[2]`)

The web source row carries useful fields beyond the four named by the recovered
mobile `Source` message. Uploaded-file rows may populate all three trailing
slots together:

| Index | Proto tag | `Source` field | Meaning |
|-------|-----------|----------------|---------|
| 5 | 6 | `download_url` | Direct download URL for the original file |
| 6 | 7 | `viewer_url` | Drive viewer URL for the original file |
| 7 | 8 | `content_mime` | MIME at blob descriptor index 2 |

The nested `SourceMetadata` row also exposes `word_count` at index 1,
`[revision_id, revision_timestamp]` at index 3, and `last_modified_at` at index
14. Their shapes and population are live-confirmed, but the mobile schema marks
the slots unused, so those semantic names are inferred and recorded as pinned
wire evidence rather than schema mappings.

### Drive Source Status Codes

| Code | `DriveSourceStatus` | Backend member |
|------|---------------------|----------------|
| 0 | *(normalized to `None`)* | `DRIVE_SOURCE_STATUS_UNSPECIFIED` |
| 1 | `INACCESSIBLE` | `DRIVE_SOURCE_STATUS_INACCESSIBLE` |
| 2 | `SYNCING` | `DRIVE_SOURCE_STATUS_SYNCING` |
| 3 | `ACTIVE` | `DRIVE_SOURCE_STATUS_ACTIVE` |
| 4 | `DELETED` | `DRIVE_SOURCE_STATUS_DELETED` |
| 5 | `GEN_AI_ACCESS_DENIED` | `DRIVE_SOURCE_STATUS_GEN_AI_ACCESS_DENIED` |

> Index 3 is absent on 405/409 rows — and proto3 omits zero-valued fields, so an
> absent slot means "no Drive claim", not "not a Drive source". The backend's
> `0` means the same thing, so the decoder normalizes it to `None` rather than
> modelling it (recorded in `ENUM_GAPS`). Only `ACTIVE` has
> been observed live; the degraded members come from the backend enum recovered
> from the official Android app (`docs/android/enums.txt`) and are pinned in
> `tests/_guardrails/_wire_contract.py`. A populated-but-unmapped code decodes to
> `DriveSourceStatus.UNKNOWN` (never `None`) and warns once (#2111).

---

## UI/Library Operation Parity

Use this table as the coverage index before adding or changing selectors. "UI
covered" means the selector or flow is documented below from the 2026-06-15 live
Chrome probe. "Library-only" means the Python API deliberately exposes a backend
or local convenience that has no stable web-control equivalent in the capture.

| Library surface | UI parity status | Notes |
|-----------------|------------------|-------|
| `NotebooksAPI.list/create/get/rename/delete/remove_from_recent` | Partial UI coverage | Home create/card/action-menu selectors are covered. Rename/delete/remove-recent are represented by project action menus and RPC payloads; destructive menu items were not re-mutated in the live probe. |
| `NotebooksAPI.get_summary/get_description/get_metadata/get_raw/get_share_url` | Library-only/read-derived | Summary content is visible in the chat panel, but these are read/format helpers rather than direct UI controls. |
| `SourcesAPI.list/get/search/add_url/add_text/add_file/add_drive/delete/rename` | UI covered | Source cards, indexed passage search, add-source modal tabs, upload/Drive entry points, source menus, and submit selectors are documented. |
| `SourcesAPI.get_guide/get_fulltext` | UI covered/read-derived | Opening a source exposes the source viewer, source guide toggle, title input, and source content; `get_fulltext()` is the programmatic extraction path. |
| `SourcesAPI.wait_*`, `refresh`, `check_freshness` | Library-only/partial UI | Wait methods are polling helpers. Refresh/freshness RPCs are documented, but no stable refresh selector was captured in the current source-list/label-list state. |
| `LabelsAPI.list/sources/generate/create/update/rename/set_emoji/add_sources/remove_sources/delete` | UI covered | Auto-label, Reorganize all sources, manual label creation, inline rename, emoji picker, delete, label panels, and source Move to label checkboxes are documented. |
| `ChatAPI.ask/get_history/session_status/cancel/delete_conversation/configure/save_answer_as_note` | UI covered | Chat input/send, live generation state/cancel, options/delete history, configure dialog, and `Save message to a note` buttons are documented. `get_conversation_id`, cache methods, and history parsing are backend/local conveniences. |
| `ArtifactsAPI.generate_*`, `suggest_reports`, `list/get/get_prompt/delete/rename/share/export` | UI covered/partial | All live Studio generation tiles and option sets are documented. Artifact list/open/menu/view-prompt/share/delete selectors are covered; export/download/retry availability depends on artifact type/status. |
| `ArtifactsAPI.download_*`, `wait_for_completion`, `poll_status`, `revise_slide`, `retry_failed` | Library-only/conditional UI | Downloads, polling, and slide revision are programmatic conveniences. Retry requires a failed artifact row; the RPC is documented but no failed-row retry selector was present in the probe. |
| `NotesAPI.list/get/create/update/delete` | UI covered/partial | Add note, note row, note view close/title input, and note menu delete are documented. Rich body editing uses NotebookLM's internal editor; keep selectors conservative. |
| `MindMapsAPI.list/generate/rename/delete/get_tree` | UI covered/partial | Interactive mind map generation is the live Studio tile. Note-backed mind maps are a synthetic/library backing; tree extraction via `GET_INTERACTIVE_HTML` is programmatic. |
| `ResearchAPI.start/poll/wait/import_sources` | UI covered for start only | Source discovery corpus/mode/submit selectors map to fast/deep web/Drive research. Polling and import verification are backend workflow helpers. |
| `SettingsAPI.get/set_output_language`, `SharingAPI.get_status/set_public/set_view_level/add_user/set_users/update_user/remove_user` | UI covered/partial | Settings and Share dialogs are covered at entry/save/copy selectors. Programmatic user-permission mutations go beyond the captured UI selectors. |
| UI-only note operations | UI-only | Note menus expose `Convert to source`, `Convert all notes to source`, `Export to Docs`, and `Export to Sheets`; keep them documented as selectors unless/until a public library method owns those flows. |

---

## Using Selector Lists

Selectors are provided as Python lists of **fallback options**. Try each in order:

```python
async def try_selectors(page, selectors: list[str], action="click", timeout=5000):
    """Try multiple selectors until one works."""
    for selector in selectors:
        try:
            element = page.locator(selector)
            if action == "click":
                await element.click(timeout=timeout)
            elif action == "fill":
                return element
            return True
        except Exception:
            continue
    raise Exception(f"None of the selectors worked: {selectors}")


# Example usage
await try_selectors(page, HOME_SELECTORS["create_notebook"])
```

---

## Home / Notebook List

### UI Selectors

```python
HOME_SELECTORS = {
    "create_notebook": [
        "button[aria-label='Create new notebook']",
        "button:has-text('Create new')",
        "mat-card[role='button']:has-text('Create new notebook')",
    ],
    "notebook_card": [
        "mat-card:has(button[aria-label='Project Actions Menu'])",
        "mat-card:has(button:has-text('more_vert'))",
        "mat-card[role='button']:has(h3)",
        "a[href*='/notebook/']",
    ],
    "notebook_menu": [
        "button[aria-label='Project Actions Menu']",
        "button[aria-label*='More options']",
        "button:has-text('more_vert')",
    ],
}
```

### Live Home Components (2026-06-15)

```python
HOME_MENU_SELECTORS = {
    "settings": "button[aria-label='Settings']",
    "search": "button[aria-label='Open search']",
    "filter_tabs": [
        "button:has-text('All')",
        "button:has-text('My notebooks')",
        "button:has-text('Featured notebooks')",
    ],
    "view_toggle": [
        "button[aria-label='Grid view']",
        "button[aria-label='List view']",
    ],
    "sort_menu": ".project-filter-button",  # e.g. "Most recent"
    "project_actions": "button[aria-label='Project Actions Menu']",
}
```

The home project action menu is now labeled `Project Actions Menu`; older
`More options` selectors did not match the live UI on 2026-06-15.

### RPC: LIST_NOTEBOOKS (wXbhsf)

**Source:** `_web/notebooks.py::WebNotebooksAPI.list()`

```python
# Minimal client builder (`NotebooksAPI.list()`), accepted by the backend:
params = [
    None,  # 0
    1,  # 1: Fixed value
    None,  # 2
    [2],  # 3: Fixed flag
]

# Live web UI/CDP capture on 2026-06-15:
params = [
    None,
    1,
    None,
    [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
    None,
    [[None, None, []], [[]], [None, []]],
]
```

### RPC: CREATE_NOTEBOOK (CCqFvf)

**Source:** `_notebooks.py::NotebooksAPI.create()`, `_web/notebooks.py::WebNotebooksAPI._send_create()`

```python
params = [
    title,  # 0: Notebook title
    None,  # 1
    None,  # 2
    [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
    # 3: Shared request-options wrapper (`build_template_block()`)
]
```

### RPC: COPY_NOTEBOOK (te3DCe)

**Server method:** `CopyProject`

**Source:** `_web/notebooks.py::WebNotebooksAPI.copy()` /
`_web/params/notebooks.py::build_copy_notebook_params()`

```python
params = [
    [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
    source_notebook_id,
    destination_title,
]
```

The response is a bare Project row and is decoded as the newly allocated
`Notebook`. Live validation on 2026-08-27 copied a notebook containing 50
sources and 5 Studio artifacts; all copied child IDs differed from the
originals. The method has no caller-supplied idempotency token, so internal
transport retry is disabled to avoid creating duplicate full copies after a
lost response.

### RPC: DELETE_NOTEBOOK (WWINqb)

**Source:** `_web/notebooks.py::WebNotebooksAPI.delete()`

```python
params = [
    [notebook_id],  # 0: Single-nested notebook ID
    [2],  # 1: Fixed flag
]
```

### RPC: GET_NOTEBOOK (rLM1Ne)

**Source:** `_web/notebooks.py::WebNotebooksAPI.get()`, `_web/sources/listing.py::SourceLister.list()`

```python
params = [
    notebook_id,  # 0
    None,  # 1
    [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
    # 2: Shared request-options wrapper
    None,  # 3
    0,  # 4: Fixed value
]
```

The slot `[2]` wrapper replaced the older bare `[2]` read-path tail. Live
capture on 2026-06-15 confirmed the nested shape in `f.req`; keep
`_web.params.notebooks.build_get_notebook_params()` and `_web/sources/listing.py` in sync.
The live web UI also sends a sixth filter/tail slot:
`[[None, None, []]]`. Initial page load used slot `[4] == 0`; a follow-up
refresh used slot `[4] == 1`. The client builder currently omits that sixth
slot because the backend accepts the compact form.

### RPC: REMOVE_RECENTLY_VIEWED (fejl7e)

**Source:** `_web/notebooks.py::WebNotebooksAPI.remove_from_recent()`

Remove a notebook from the recently viewed list (doesn't delete the notebook).

```python
params = [notebook_id]  # Just the notebook ID

# No source_path needed
await rpc_call(
    RPCMethod.REMOVE_RECENTLY_VIEWED,
    params,
    allow_null=True,
)

# Response: None (no return value)
```

---

## Sources Panel

### UI Selectors

```python
SOURCES_SELECTORS = {
    "add_sources": [
        "button[aria-label='Add source']",
        "button:has-text('+ Add sources')",
        "button:has-text('Add sources')",
    ],
    "source_card": ".single-source-container",
    "source_menu": [
        "button.source-item-more-button[aria-label='More']",
        "button[aria-label*='More options']",
    ],
    "remove_source": "button:has-text('Remove source')",
    "rename_source": "button:has-text('Rename source')",
}

ADD_SOURCE_MODAL = {
    "modal": "mat-dialog-container[role='dialog']",
    "upload_files": "button:has-text('Upload files')",
    "website_tab": [
        "button:has-text('Websites')",
        "button:has-text('Website')",
    ],
    "drive_tab": "button:has-text('Drive')",
    "url_input": [
        "textarea[aria-label='Enter URLs']",
        "textarea[placeholder='Paste any links']",
        "textarea[placeholder*='links']",
    ],
    "copied_text_tab": "button:has-text('Copied text')",
    "copied_text_input": [
        "textarea[aria-label='Pasted text']",
        "textarea[placeholder='Paste text here']",
    ],
    "back_button": "button[aria-label='Back']",
    "close_button": "button[aria-label='Close']",
    "submit_button": "button:has-text('Insert')",
}

SOURCE_DISCOVERY_SELECTORS = {
    "query": [
        "textarea[placeholder='Search the web for new sources']",
        "textarea[aria-label='Discover sources based on the inputted query']",
    ],
    "corpus_menu": ".corpus-menu-trigger",
    "corpus_web": ".corpus-option-web",
    "corpus_drive": ".corpus-option-drive",
    "research_mode_menu": ".researcher-menu-trigger",
    "fast_research": ".research-option-fast-research",
    "deep_research": ".research-option-deep-research",
    "submit": "button.actions-enter-button[aria-label='Submit']",
    "auto_label": [
        "button[aria-label='Auto-label your sources by topic']",
        "button[aria-label='Undo or re-label sources']",
    ],
}

DRIVE_PICKER_SELECTORS = {
    "picker_iframe": "iframe[src*='picker'], iframe[src*='docs.google']",
}

SOURCE_VIEWER_SELECTORS = {
    "open_source": "button.source-stretched-button",
    "viewer": "source-viewer",
    "title_input": "source-viewer input.title-input",
    "close_view": [
        "source-viewer button[mattooltip='Close source view']",
        "button[mattooltip='Close source view']",
    ],
    "open_in_new": "source-viewer button[aria-label='Open in new tab']",
    "source_guide_toggle": "source-viewer button[aria-label='Close source guide']",
    "guide_keyword_chip": "source-viewer button[role='option']",
}
```

Live source-add submit check (2026-06-15): copied text, Website URL
(`https://example.com`), and file upload all closed the modal and increased the
source-card count. Drive opens a Google picker iframe; selecting a Drive file
was not needed to verify the entry point.

Live source-viewer check (2026-06-15): opening a source showed
`source-viewer`, `input.title-input`, `Open in new tab`, a `Close source guide`
toggle, source-guide keyword chips with `role='option'`, and a tooltiped close
button (`mattooltip='Close source view'`).

### RPC: ADD_SOURCE (izAoDd) - URL

**Sources:** `_web/sources/add.py::SourceAddService.add_url_source()` (single item),
`_web/sources/batch.py::SourceBatchAddService.add_urls()` (true batch)

```python
# URL goes at position [2] in an 11-element source spec.
params = [
    [[None, None, [url], None, None, None, None, None, None, None, 1]],
    notebook_id,  # 1: Notebook ID
    [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
    # 2: Shared request-options wrapper
]
```

The existing MCP `source_add(urls=[...])` and REST `/sources/batch` endpoints
put multiple URL specs in `params[0]` and issue this RPC once. `AddSources` is
per-item rather than atomic: successful Source rows remain in request order,
while failed entries are silently omitted unless every entry fails (then the
RPC raises). The adapters reconcile omissions with an ERROR-status source list
and restore positional result rows. This true-batch path disables transport
retries because a timeout leaves the committed subset unknown; the ordinary
single-item `sources.add_url()` path still uses its dedicated probe-then-create
recovery unchanged.

### RPC: ADD_SOURCE (izAoDd) - Text

**Source:** `_web/sources/add.py::SourceAddService.add_text()`

```python
# [title, content] at position [1] in an 11-element source spec; slot [3] is
# the captured source-type code for pasted text.
params = [
    [[None, [title, content], None, 2, None, None, None, None, None, None, 1]],
    notebook_id,
    [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
]
```

### RPC: ADD_SOURCE (izAoDd) - YouTube

**Source:** `_web/sources/add.py::SourceAddService.add_youtube_source()`

```python
# YouTube URL at position [7] in the source spec (different from regular URL).
params = [
    [[None, None, None, None, None, None, None, [url], None, None, 1]],  # 0
    notebook_id,  # 1
    [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
    # 2: Shared wrapper
]
```

### RPC: ADD_SOURCE (izAoDd) - Google Drive

**Source:** `_web/sources/add.py::SourceAddService.add_drive()`

```python
# Drive source structure - single-wrapped (not double!)
source_data = [
    [file_id, mime_type, 1, title],  # 0: File info
    None,
    None,
    None,
    None,
    None,  # 1-5: Padding
    None,
    None,
    None,
    None,  # 6-9: Padding
    1,  # 10: Trailing flag
]
params = [
    [source_data],  # 0: Single-wrapped (NOT [[source_data]])
    notebook_id,  # 1: Notebook ID
    [2],  # 2: Source type flag
    [1, None, None, None, None, None, None, None, None, None, [1]],  # 3: Config
]
```

**Note:** Drive add is intentionally still on the older `[2]`,
`[1, ..., [1]]` tail pending a fresh live Drive capture. URL, YouTube, text,
CREATE_NOTEBOOK, and ADD_SOURCE_FILE use the shared nested wrapper from
`_web/params/sources.py::build_template_block()`.

### RPC: ADD_SOURCE_FILE (o4cbdc) - File Upload Registration

**Source:** `_web/sources/upload.py::SourceUploadPipeline.register_file_source()`,
`_web/params/sources.py::build_register_file_source_params()`,
`_web/params/sources.py::build_resumable_upload_start_request()`

File uploads are a two-step flow. First, `ADD_SOURCE_FILE` registers the file
source and returns a `SOURCE_ID`; then the client starts a Scotty resumable
upload session and streams the bytes to the `x-goog-upload-url` returned by
that start request.

```python
params = [
    [[filename]],  # 0: Filename wrapped twice
    notebook_id,  # 1: Notebook ID
    [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
    # 2: Shared request-options wrapper
]

# Called with source_path:
await rpc_call(
    RPCMethod.ADD_SOURCE_FILE,
    params,
    source_path=f"/notebook/{notebook_id}",
    allow_null=False,
    disable_internal_retries=True,
)
```

Registration is mutating, so the upload pipeline uses the same
probe-then-create idempotency pattern as URL and Drive sources. Because
filenames are not unique, the probe records source IDs before the create and
only trusts a same-title source if it is new since that baseline.

```python
# Start resumable upload after SOURCE_ID registration:
start_headers = {
    "Accept": "*/*",
    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    "Origin": base_url,
    "Referer": f"{base_url}/",
    "x-goog-authuser": authuser,
    "x-goog-upload-command": "start",
    "x-goog-upload-header-content-length": str(file_size),
    "x-goog-upload-header-content-type": content_type,
    "x-goog-upload-protocol": "resumable",
}
start_body = {
    "PROJECT_ID": notebook_id,
    "SOURCE_NAME": filename,
    "SOURCE_ID": source_id,
}

# Stream bytes to response.headers["x-goog-upload-url"]:
upload_headers = {
    "Accept": "*/*",
    "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
    "x-goog-authuser": authuser,
    "Origin": base_url,
    "Referer": f"{base_url}/",
    "x-goog-upload-command": "upload, finalize",
    "x-goog-upload-offset": "0",
}
```

### RPC: DELETE_SOURCE (tGMBJ)

**Source:** `_web/sources/__init__.py::WebSourcesAPI.delete()`

**IMPORTANT:** `notebook_id` is passed via `source_path`, NOT in params!

```python
params = [[[source_id]]]  # Triple-nested!

# Called with:
await rpc_call(
    RPCMethod.DELETE_SOURCE,
    params,
    source_path=f"/notebook/{notebook_id}",  # <-- notebook_id here
)
```

### RPC: UPDATE_SOURCE / Rename (b7Wfje)

**Source:** `_web/sources/__init__.py::WebSourcesAPI.rename()`

```python
# Different structure: None at [0], source_id at [1], title triple-nested at [2]
params = [
    None,  # 0
    [source_id],  # 1: Single-nested source ID
    [[[new_title]]],  # 2: Triple-nested title
]
```

### RPC: GET_SOURCE_GUIDE (tr032e)

**Source:** `_web/sources/content.py::SourceContentRenderer.get_guide()`

```python
# Quadruple-nested source ID!
params = [[[[source_id]]]]
```

### RPC: GET_SOURCE (hizoJc)

**Source:** `_web/sources/content.py::get_fulltext()`

**Purpose:** Get raw text or clean HTML/markdown content of a source.

**Params:**
```python
# Position 0: Single-nested source ID
# Position 1: Output type: [2] for plain text, [3] for cleaned HTML/markdown structure
# Position 2: Format selector matching position 1
params = [
    [source_id],  # 0
    [2],  # 1
    [2],  # 2
]

# Markdown/HTML source rendering uses the same selector in both slots:
params = [
    [source_id],
    [3],
    [3],
]
```

**Request format:**
```python
await rpc_call(
    RPCMethod.GET_SOURCE,
    params,
    source_path=f"/notebook/{notebook_id}",
)
```

### RPC: RETRIEVE_RELEVANT_CHUNKS (ASU5Oe)

**Source:** `_web/sources/search.py::SourceSearchService.search()`

**Purpose:** Search the indexed source corpus and return globally ranked source
passages. An omitted or empty source filter searches the whole notebook.

```python
params = [
    notebook_id,
    query,
    None,
    [1],
]

# Optional source filter at position 4:
params.append([[[source_id] for source_id in source_ids]])

await rpc_call(
    RPCMethod.RETRIEVE_RELEVANT_CHUNKS,
    params,
    source_path=f"/notebook/{notebook_id}",
    allow_null=True,
    raise_on_null_status=True,
)
```

The live response is `[[[source_id, [chunk, ...]], ...]]]`, where each chunk is
`[[[[text_part, ...]]], rank, [[None, start, end], ...]]`. The decoder joins
text parts and returns `RelevantChunk` values. This rpcid is registered by a
lazy feature module, so the eager-homepage registry monitor reports it as
`LAZY-MODULE`; the direct read-only RPC health probe remains its rotation
canary.

---

## Source Labels

Source labels group a notebook's sources into AI-generated (or manually named)
topic buckets. A label is a standalone entity — a source carries no
back-reference; the label owns a list of source IDs, and membership is
many-to-many (a source can belong to multiple labels). Every label RPC's first
argument is the recurring request-options wrapper used by `_web/settings.py`:

```python
OPTS = [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]]
```

### The Label Tuple (response shape)

Each label is a 4-tuple `[name, sources, label_id, emoji]`:

| Slot | Field | Notes |
|------|-------|-------|
| `[0]` | `name` | str |
| `[1]` | `sources` | `[[source_id], ...]` (each UUID wrapped in its own 1-element list); **`None`** for a new empty label |
| `[2]` | `label_id` | server-assigned UUID |
| `[3]` | `emoji` | `""` when unset, else the emoji string |

**Response envelopes differ by RPC:** `CREATE_LABEL` returns
`[None, [label, ...]]` (label set at index `[1]`); `LIST_LABELS` returns
`[[label, ...]]` (label set at index `[0]`); `UPDATE_LABEL` and `DELETE_LABEL`
echo `[]` on success.

### UI Selectors

Live-checked on 2026-06-15 after NotebookLM generated source labels for the
probe notebook.

```python
LABEL_UI_SELECTORS = {
    # Before labels exist, the same button is labelled "Auto-label your sources
    # by topic"; after labels exist, it becomes "Undo or re-label sources".
    "auto_label_menu": [
        "button[aria-label='Auto-label your sources by topic']",
        "button[aria-label='Undo or re-label sources']",
    ],
    "add_new_label": "button[role='menuitem']:has-text('Add new label')",
    "reorganize": "button[role='menuitem']:has-text('Reorganize')",
    "reorganize_all_sources": "button[role='menuitem']:has-text('All sources')",
    "return_to_list_view": "button[role='menuitem']:has-text('Return to list view')",
    "label_panel": "source-picker mat-expansion-panel",
    "label_header": "source-picker mat-expansion-panel-header",
    "label_name": "source-picker .label-name",
    "label_checkbox": "source-picker input[aria-label='<label name>']",
    "label_more": "source-picker button.label-more-button",
    "label_rename": "button[role='menuitem']:has-text('Rename')",
    "label_remove": "button[role='menuitem']:has-text('Remove')",
    "label_add_emoji": "button[role='menuitem']:has-text('Add emoji')",
    "label_rename_input": "source-picker input.label-rename-input",
    "emoji_search": "input[aria-label='Search for an emoji, press Escape to dismiss.']",
    "source_move_to": "button.more-menu-move-to-labels-button",
    "move_to_label_option": ".cdk-overlay-container [role='menuitem']:has-text('<label name>')",
    "move_to_checkbox": ".cdk-overlay-container input.mdc-checkbox__native-control",
}
```

Live label-flow notes:

- `Add new label` immediately creates a manual label named `New Label`; there is
  no pre-create dialog.
- `Rename` switches that label row into an inline
  `input.label-rename-input`; submitting with Enter commits the rename.
- `Add emoji` opens the inline emoji picker and exposes the emoji search input.
- `Remove` deleted the throwaway label immediately in the live UI probe.
- Expanding a label panel reveals its member source rows; each source row keeps
  the normal source menu plus a label-specific `Move to` submenu.
- `Move to` shows every current label with checkboxes. The current label is
  checked; unchecked labels can be selected to change/add membership.

### RPC: CREATE_LABEL (agX4Bc)

**Source:** `_web/labels.py::generate()`, `_web/labels.py::create()` (builders in `_web/params/labels.py`)

A single multi-mode RPC; the mode is selected by which slot is populated. Slot
`[4]` drives AI auto-labeling (`generate`); slot `[5]` creates manual labels
(`create`).

```python
# Auto-label / Reorganize -> All sources (slot [4] = []) - WIPES + regenerates with new ids
params = [OPTS, notebook_id, None, None, []]

# Reorganize -> Unlabeled sources (slot [4] = [0]) - preserves existing labels
params = [OPTS, notebook_id, None, None, [0]]

# Manual create (slot [5] = [[name, emoji]])
params = [OPTS, notebook_id, None, None, None, [["New Label", ""]]]
```

**Response (all modes):** the full post-op label set — `[None, [label, ...]]`.

### RPC: LIST_LABELS (I3xc3c)

**Source:** `_web/labels.py::list()`

```python
params = [OPTS, notebook_id]
```

**Response:** `[[label, ...]]` — a single-element outer list wrapping the labels
(**not** `[None, [label, ...]]` like `agX4Bc`). Each label's slot `[1]` carries
its source UUIDs, so one `list()` call gives the complete source→label mapping.

### RPC: UPDATE_LABEL (le8sX)

**Source:** `_web/labels.py::update()`, `rename()`, `set_emoji()`,
`add_sources()`, `remove_sources()` (builder: `_web/params/labels.py`)

A unified label-update RPC covering rename, emoji, and source membership. Slot
`[3]` is a fieldmask `[[name_emoji, sources_add, sources_remove]]`; populate
only the group(s) you want to change.

```python
# Rename (name_emoji = [name]; sources omitted)
params = [OPTS, notebook_id, label_id, [[[new_name]]]]

# Set emoji (name slot None, emoji set; sources omitted)
params = [OPTS, notebook_id, label_id, [[[None, emoji]]]]

# Add source(s) (name_emoji None, sources set) - APPENDS, does not replace
params = [OPTS, notebook_id, label_id, [[None, [[source_id]]]]]

# Remove source from this label only (sources_add None, sources_remove set)
params = [OPTS, notebook_id, label_id, [[None, None, [[source_id]]]]]
```

**Note:** the `sources` group **appends** (send only the IDs to add — existing
members survive) and labels may **overlap** (adding a source does not remove it
from any other label). Source **removal** is supported via the `UPDATE_LABEL`
fieldmask's `sources_remove` slot (`[3][0][2]`) — un-assigning the source from
this label only (it stays in the notebook and in any other label). The current
API loops one RPC per unique source id for add/remove membership changes; the
wire shape honors only the first id in each group.

**Response:** `[]` on success.

### RPC: DELETE_LABEL (GyzE7e)

**Source:** `_web/labels.py::delete()`

Batch-capable — label IDs are passed as an array. Deleting a label does **not**
delete its sources (they become unlabeled).

```python
params = [OPTS, notebook_id, [label_id, ...]]
```

**Response:** `[]` on success.

---

## Chat Panel

### UI Selectors

```python
CHAT_SELECTORS = {
    "message_input": [
        "textarea[placeholder='Ask a question or create something']",
        "textarea[aria-label='Query box']",
        "textarea[placeholder='Start typing...']",
    ],
    "send_button": "button.submit-button[aria-label='Submit']",
    "configure_button": "button[aria-label='Configure notebook']",
    "chat_history": [
        "chat-panel .chat-panel-content",
        "[role='log']",
    ],
    "message_bubble": [
        ".to-user-container",  # AI messages
        ".from-user-container",  # User messages
    ],
    "save_to_note": "button[aria-label='Save message to a note']",
    "copy_response": "button[aria-label='Copy model response to clipboard']",
    "rate_good": "button[aria-label='Rate response as good']",
    "rate_bad": "button[aria-label='Rate response as bad']",
}

CHAT_CONFIG = {
    "modal": "configure-notebook-settings",
    "goal_default": (
        "configure-notebook-settings .prompt-section-toggles button[aria-label='Default button']"
    ),
    "goal_learning_guide": (
        "configure-notebook-settings .prompt-section-toggles "
        "button[aria-label='Learning Guide prompt button']"
    ),
    "goal_custom": (
        "configure-notebook-settings .prompt-section-toggles button[aria-label='Custom button']"
    ),
    "length_default": (
        "configure-notebook-settings .style-section-toggles button[aria-label='Default button']"
    ),
    "length_shorter": "configure-notebook-settings button[aria-label='Concise style guide button']",
    "length_longer": "configure-notebook-settings button[aria-label='Verbose style guide button']",
    "save_button": "configure-notebook-settings button[aria-label*='Save settings']",
    "close_button": "button[aria-label='Close chat settings']",
}

CHAT_OPTIONS = {
    "menu_button": "button[aria-label='Chat options']",
    "customize_notebook": "button[role='menuitem']:has-text('Customize notebook')",
    "delete_history": "button[role='menuitem']:has-text('Delete chat history')",
}

NOTEBOOK_SHELL_SELECTORS = {
    "share_notebook": "button[aria-label='Share notebook']",
    "share_dialog": "mat-dialog-container[role='dialog']:has-text('Share')",
    "share_copy_link": "button:has-text('Copy link')",
    "share_more_copy_options": "button[aria-label='More copy options']",
    "share_save": "button:has-text('Save')",
    "settings_menu": "button[aria-label='Settings']",
    "settings_output_language": "button[role='menuitem']:has-text('Output Language')",
    "settings_theme": "button[role='menuitem'][aria-label='Theme']",
}
```

### Query Endpoint (Streaming)

Chat queries use a **separate streaming endpoint**, not batchexecute:

```
POST /_/LabsTailwindUi/data/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GenerateFreeFormStreamed
```

#### Streamed response envelope (`GenerateFreeFormStreamedResponse`)

Each `wrb.fr` frame's inner JSON decodes to this envelope. Chunks arrive
**cumulatively** — every chunk carries the answer text so far.

| Index | Proto tag | Field | Decoded as |
|-------|-----------|-------|------------|
| 0 | 1 | `answer` (`AnswerResponse`) | `AnswerRow` |
| 4 | 5 | `isFinalResponse` | `StreamEnvelopeRow.is_final_response` |

`isFinalResponse` is `true` on **exactly the last chunk** — `false` on every
other one, across a 5-chunk and a 6-chunk live stream (#2122) and on all 9 asks
of the 2026-08-07 audit. `parse_streaming_chat_response` uses it to select the
answer; the historical longest-wins heuristic is the fallback, and logs a
`WARNING` when it fires. Heartbeat frames decode to `[]` and answer `false`.

#### `AnswerResponse.conversationTurnKey` (`answer_row[2]`)

Populated on every chunk of every ask. `SubmitFeedbackRequest.conversationTurnKey`
(tag 1) is the only consumer of this message in the recovered schema. Surfaced
as `AskResult.turn_key`.

| Index | Proto tag | Proto name | Public attribute | Live observation |
|-------|-----------|------------|------------------|------------------|
| 0 | 1 | `sessionId` | `session_id` | Mixed — see below. The same slot `AnswerRow.server_conversation_id` reads |
| 1 | 2 | `conversationId` | `turn_id` | A **different** UUID on each turn — identifies the turn, not the conversation |
| 2 | 3 | `fieldType` | `turn_code` | `2187103311` / `3083048340` / `2502166488` — one per turn, constant across that turn's chunks |

> **Slot 0 keeps its proto name because the evidence about it is mixed.** A
> live two-turn probe (2026-08-13) saw the `hPTbtc`-resolved conversation id
> here, identical on both turns. This repo's own recorded cassettes show it
> **differing** from the recorded `hPTbtc` id in 4/4 chat captures
> (`chat_ask.yaml`: slot 0 is `cf23c9a5-…`, `hPTbtc` returns `bc0666c8-…`). It
> is the same slot issue #659 established is a per-stream identifier — `khqZz`
> returns 0 turns for it, and replaying it as `params[4]` produces a ghost
> turn. So nothing is claimed for it, `ask()` still resolves its conversation
> id through `hPTbtc`, and callers should use `AskResult.conversation_id`.
>
> **Slot 1 does NOT keep its proto name**, because `conversationId` contradicts
> every observation: it changes per turn. `fieldType` is likewise the schema
> extractor's placeholder for a name it could not recover, and the observed
> values are not type tags — so `turn_code` is carried verbatim and not
> interpreted. The wire↔attribute mapping is pinned in
> `tests/_guardrails/_wire_contract.py`.
>
> **There is no per-turn delete RPC to address with this key.**
> `DeleteChatTurnsRequest` takes `requestContext` / `chatSessionId` /
> `deleteAllHistory` — it deletes whole histories and carries no turn key.

### RPC: RENAME_NOTEBOOK (s0tc2d) - Rename Only

**Source:** `_notebooks.py::NotebooksAPI.rename()`, `_web/notebooks.py::WebNotebooksAPI.update()`

```python
# Just rename, no chat config
params = [
    notebook_id,  # 0
    [[None, None, None, [None, new_title]]],  # 1: Nested title at [[[3][1]]]
]
```

### RPC: RENAME_NOTEBOOK (s0tc2d) - Configure Chat

**Source:** `_web/chat.py::WebChatAPI.configure()`

```python
# Chat goal codes (ChatGoal enum)
CHAT_GOAL_DEFAULT = 1
CHAT_GOAL_CUSTOM = 2
CHAT_GOAL_LEARNING_GUIDE = 3

# Response length codes (ChatResponseLength enum)
CHAT_LENGTH_DEFAULT = 1
CHAT_LENGTH_LONGER = 4
CHAT_LENGTH_SHORTER = 5

# Build goal array
goal_array = [goal_value]  # e.g., [1] for DEFAULT
# For CUSTOM: goal_array = [2, custom_prompt]

chat_settings = [goal_array, [response_length_value]]

params = [
    notebook_id,  # 0
    [[None, None, None, None, None, None, None, chat_settings]],  # 1: Settings at [[[7]]]
]
```

### RPC: GET_LAST_CONVERSATION_ID (hPTbtc)

**Source:** `_web/chat.py::WebChatAPI.get_conversation_id()`

Returns the most recent conversation ID for a notebook. The server always returns
exactly one ID regardless of the `limit` param. Use `GET_CONVERSATION_TURNS` to
fetch the actual messages for the returned conversation.

```python
params = [
    [],  # 0: Empty sources array
    None,  # 1
    notebook_id,  # 2
    1,  # 3: Limit (server ignores this; always returns one ID)
]

# Live web UI/CDP capture on 2026-06-15:
params = [
    [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
    None,
    notebook_id,
    20,
]
```

**Response:** `[[[conv_id]]]` — single entry list containing the conversation ID.

---

### RPC: GET_CONVERSATION_TURNS (khqZz)

**Source:** `_web/chat.py::WebChatAPI.get_conversation_turns()`

Returns the Q&A turns for a specific conversation. Turns are ordered newest-first.

```python
params = [
    [],  # 0: Empty
    None,  # 1
    None,  # 2
    conversation_id,  # 3
    limit,  # 4: Max turns to return (e.g., 2 for latest Q&A pair)
]

# Live web UI/CDP capture on 2026-06-15:
params = [
    [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
    None,
    None,
    conversation_id,
    20,
]
```

**Response turn structure:**
- `turn[2] == 1`: User question — text is at `turn[3]`
- `turn[2] == 2`: AI answer — text is at `turn[4][0][0]`

---

### RPC: GET_CHAT_SESSION_STATUS (oXwmh)

**Source:** shared `_chat.py::ChatAPI.session_status()` selection and
`_web/chat.py::WebChatAPI._get_session_status()` send/decode.

```python
params = [
    None,  # 0: reserved
    conversation_id,  # 1: chat session id
]
```

**Response:** `[None, 1]` when idle; `[generation_token, 2]` while generating.
Unknown status codes or a missing token in state 2 fail closed as wire drift.

---

### RPC: CANCEL_GENERATION (XgrPMd)

**Source:** shared `_chat.py::ChatAPI.cancel()` selection and
`_web/chat.py::WebChatAPI._cancel_generation()` send.

```python
params = [
    None,  # 0: reserved
    conversation_id,  # 1: chat session id
]
```

**Response:** empty `[]`. Generation stops, but an already-open Web streaming
HTTP response does not close; its owner must abandon the local stream after
this RPC succeeds.

---

### RPC: DELETE_CONVERSATION (J7Gthc)

**Source:** shared `_chat.py::ChatAPI.delete_conversation()` orchestration,
with the Web RPC send in `_web/chat.py::WebChatAPI._send_delete_conversation()`

Deletes a conversation server-side. Mirrors the NotebookLM web UI's "Delete
history" button. After the call, the next `ask()` with no `conversation_id`
starts a brand-new conversation instead of extending the deleted one.

```python
params = [
    [],  # 0: Empty / reserved
    conversation_id,  # 1: Conversation to delete
    None,  # 2
    1,  # 3: Always observed as 1; meaning unconfirmed
]
# source_path = f"/notebook/{notebook_id}"  — notebook scope rides on the URL
```

**Response:** empty `[]` body inside the standard `wrb.fr` envelope. Success
is signaled by the absence of an error — there is no return payload.

---

### RPC: SUGGEST_PROMPTS (otmP3b)

**Source:** `_web/notebooks.py::WebNotebooksAPI.suggest_prompts()`

Returns AI-suggested prompts for a notebook (the live
`GeneratePromptSuggestions` method) — a general notebook-prompt endpoint whose
`mode` selects the product surface (default `4` suggests chat questions). Each
suggestion pairs a short title with a ready-to-send multi-line instruction
string. Shape live-verified on the consumer/labs cohort (issue #1612) — the
backend serves it regardless of the web UI's experiment flag.

```python
params = [
    # 0: client context (capability envelope; same family as artifact RPCs)
    [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
    notebook_id,  # 1: Notebook to suggest prompts for
    [[source_id], ...],  # 2: Source-id wrappers (one [id] per source)
    mode,  # 3: REQUIRED int "mode/surface" enum (1..10;
    #    0/omitted -> server INTERNAL). Default 4.
    None,  # 4: Reserved (always null)
    query,  # 5: Optional free-text steer (or null)
]
# source_path = f"/notebook/{notebook_id}"
```

**Response:** a single-element envelope wrapping the suggestion rows —
`[[ [title, prompt], [title, prompt], ... ]]`. Each row is decoded to a
`PromptSuggestion(title, prompt)`. An empty / degenerate payload yields `[]`
(suggestions are best-effort, so an absent payload does not raise).

---

## Studio Panel - Artifact Generation

### UI Selectors

```python
STUDIO_SELECTORS = {
    "artifact_button": ".create-artifact-button-container",
    "customize_icon": [
        ".option-icon",
        "button.edit-button[aria-label^='Customize']",
    ],  # Click THIS for customization!
    "add_note": "button:has-text('Add note')",
    "artifact_list": ".artifact-library-container",
    "artifact_row": ".artifact-item-button",
    "artifact_menu": ".artifact-more-button",
}

ARTIFACT_MENU = {
    "rename": "button:has-text('Rename')",
    "share": "button:has-text('Share')",
    "view_prompt_sources": "button:has-text('View prompt and sources')",
    "delete": "button:has-text('Delete')",
}

ARTIFACT_VIEWER_SELECTORS = {
    "open_artifact": ".artifact-item-button",
    "expand": "button[aria-label='Expand']",
    "more_options": "button[aria-label='More options']",
    "view_sources": "button:has-text('View'):has-text('sources')",
    "good_content": "button[aria-label='Good content rating']",
    "bad_content": "button[aria-label='Bad content rating']",
}

NOTE_SELECTORS = {
    "add_note": "button.add-note-button, button:has-text('Add note')",
    "note_row": ".artifact-item-button:has-text('sticky_note_2')",
    "note_row_by_title": ".artifact-item-button:has-text('<note title>')",
    "open_note": ".artifact-item-button:has-text('<note title>') .artifact-stretched-button",
    "note_menu": ".artifact-item-button:has-text('<note title>') .artifact-more-button",
    "note_view_title": "input.title-input",
    "note_view_close": "button[aria-label='Close note view']",
}

NOTE_MENU = {
    "convert_to_source": "button[role='menuitem']:has-text('Convert to source')",
    "convert_all_notes_to_source": "button[role='menuitem']:has-text('Convert all notes to source')",
    "export_to_docs": "button[role='menuitem']:has-text('Export to Docs')",
    "export_to_sheets": "button[role='menuitem']:has-text('Export to Sheets')",
    "delete": "button[role='menuitem']:has-text('Delete')",
    "confirm_delete": "button[aria-label='Confirm deletion']",
    "cancel_delete": "button[aria-label='Cancel']",
}

GENERATION_TILE_SELECTORS = {
    "tile": ".create-artifact-button-container",
    "tile_by_label": ".create-artifact-button-container[aria-label='<label>']",
    "customize_icon": ".create-artifact-button-container[aria-label='<label>'] .option-icon",
    # Present for every live tile except Video Overview and Reports:
    "customize_button": "button.edit-button[aria-label^='Customize']",
    "customizer_dialog": "mat-dialog-container[role='dialog']",
    "dialog_close": [
        "button[aria-label='Close dialog']",
        "button[aria-label='Close']",
    ],
    "language_select": "mat-select[role='combobox']",
    "language_option": "mat-option[role='option']",
    "dialog_generate": "button:has-text('Generate')",
}
```

Live Studio tiles on 2026-06-15:

| Tile label | Customize selector | Live options |
|------------|--------------------|--------------|
| Audio Overview | `.create-artifact-button-container[aria-label='Audio Overview'] .option-icon` | Format: Deep Dive, Brief, Critique, Debate; language; Length: Short, Default, Long; prompt textarea `aria-label='What should the AI hosts focus on in this episode?'` |
| Slide Deck | `.create-artifact-button-container[aria-label='Slide Deck'] .option-icon` | Format: Detailed Deck, Presenter Slides; language; Length: Short, Default; prompt textarea `aria-label='Describe the slide deck you want to create'` |
| Video Overview | `.create-artifact-button-container[aria-label='Video Overview'] .option-icon` | Format: Cinematic, Explainer, Brief, Short; prompt textarea `aria-label='How would you like the video to be customized?'` |
| Mind Map | `.create-artifact-button-container[aria-label='Mind Map'] .option-icon` | Prompt textarea `aria-label='Text area for custom topic'` |
| Reports | `.create-artifact-button-container[aria-label='Reports'] .option-icon` | Root format picker: Create Your Own, Briefing Doc, Study Guide, Blog Post, plus dynamic suggested formats; each format can open a report prompt form |
| Flashcards | `.create-artifact-button-container[aria-label='Flashcards'] .option-icon` | Number of Cards: Fewer, Standard (Default), More; Difficulty: Easy, Medium (Default), Hard; prompt textarea `aria-label='Text area for custom topic'` |
| Quiz | `.create-artifact-button-container[aria-label='Quiz'] .option-icon` | Number of Questions: Fewer, Standard (Default), More; Difficulty: Easy, Medium (Default), Hard; prompt textarea `aria-label='Text area for custom topic'` |
| Infographic | `.create-artifact-button-container[aria-label='Infographic'] .option-icon` | Orientation: Landscape, Portrait, Square; visual style: Auto-select, Kawaii, Clay, Sketch Note, Anime, Editorial, Instructional, Bento Grid, Bricks, Scientific, Professional; detail: Concise, Standard, Detailed BETA; prompt textarea `aria-label='Describe the infographic you want to create'` |
| Data Table | `.create-artifact-button-container[aria-label='Data Table'] .option-icon` | Language; prompt textarea `aria-label='Describe the data table you want to create'` |

Reports-specific live selectors:

```python
REPORT_DIALOG_SELECTORS = {
    "create_your_own": "button[aria-label='Create Your Own']",
    "briefing_doc": "button[aria-label='Briefing Doc']",
    "study_guide": "button[aria-label='Study Guide']",
    "blog_post": "button[aria-label='Blog Post']",
    "suggested_format": "button.primary-action-button[aria-label]",  # Dynamic titles
    "customize_report": "button[aria-label='Customize Report']",
    "back": "button[aria-label='Back']",
    "prompt": "textarea[aria-label='Input to describe the kind of report to create']",
}
```

On 2026-06-15 the live suggested report titles for the probe notebook were
`Product Evolution Report`, `Strategic Adoption Analysis`,
`Technical Terminology Reference`, and `Capability Application Overview`.
These are notebook-content-dependent, so treat them as examples, not constants.

Notes live in the same Studio list as artifacts. Live note probe
(2026-06-15): clicking `Add note` created a `New Note` row with
`.artifact-item-button` and `.artifact-more-button`. Its menu exposed
`Convert to source`, `Convert all notes to source`, `Export to Docs`,
`Export to Sheets`, and `Delete`; delete opened a confirmation dialog with
`button[aria-label='Confirm deletion']`. The throwaway `New Note` from the
probe was deleted after collecting selectors.

### Critical: Edit Icon vs Full Button

```python
# ✅ Click edit icon for customization dialog
await page.locator(
    ".create-artifact-button-container[aria-label='Audio Overview'] .option-icon"
).click()

# Video Overview and Reports also use `.option-icon`, but do not expose it as
# `button.edit-button[aria-label^='Customize']` in the live DOM.
await page.locator(
    ".create-artifact-button-container[aria-label='Video Overview'] .option-icon"
).click()

# ❌ Clicking full button starts generation with defaults (skips customization!)
await page.locator(".create-artifact-button-container[aria-label='Audio Overview']").click()
```

### RPC: CREATE_ARTIFACT (R7cb6c)

**All artifact types use `R7cb6c` with different content type codes and nested configs.**

**Source:** `_artifacts.py` and `_web/artifact/generation.py` (param builders: `_web/params/artifacts.py`)

Live UI captures on 2026-06-15 for Data Table and interactive Mind Map showed
the web client sending the full client-options block below as param `0` (not
the older minimal `[2]` form). The Python payload builders now match this UI
shape for every `CREATE_ARTIFACT` generator and for `RETRY_ARTIFACT`.

```python
create_artifact_options = [
    2,
    None,
    None,
    [1, None, None, None, None, None, None, None, None, None, [1]],
    [[1, 4, 8, 2, 3, 6]],  # artifact-type capability list
]
```

Kickoff response shape live-confirmed on 2026-06-15 for Data Table and
interactive Mind Map:

```python
result = [
    [
        artifact_id,  # [0][0]: task/artifact id
        title,  # [0][1]
        artifact_type,  # [0][2]
        None,  # [0][3]
        status_code,  # [0][4]: 1 in both captures = ARTIFACT_STATUS_INITIALIZED ("pending")
        # ... additional artifact metadata slots; first row len was 20
    ]
]
```

`ArtifactGenerationService._parse_generation_result()` reads `result[0][0]`
and `result[0][4]`, which matches both live responses.

#### Audio Overview (Type 1)

**Source:** `_artifacts.py::ArtifactsAPI.generate_audio()` orchestration;
`_web/artifact/generation.py` RPC dispatch (param builders: `_web/params/artifacts.py`)

```python
source_ids_triple = [[[sid]] for sid in source_ids]  # [[[s1]], [[s2]], ...]
source_ids_double = [[sid] for sid in source_ids]  # [[s1], [s2], ...]

params = [
    create_artifact_options,  # 0: Client options/capabilities
    notebook_id,  # 1
    [
        None,  # [0]
        None,  # [1]
        1,  # [2]: ArtifactTypeCode.AUDIO
        source_ids_triple,  # [3]
        None,  # [4]
        None,  # [5]
        [
            None,
            [
                instructions,  # Focus/instructions text
                length_code,  # 1=SHORT, 2=DEFAULT, 3=LONG
                None,
                source_ids_double,
                language,  # "en"
                None,
                format_code,  # 1=DEEP_DIVE, 2=BRIEF, 3=CRITIQUE, 4=DEBATE
            ],
        ],  # [6]
    ],  # 2: Source config
]
```

#### Video Overview (Type 3)

**Source:** `_artifacts.py::ArtifactsAPI.generate_video()` orchestration;
`_web/artifact/generation.py` RPC dispatch (param builders: `_web/params/artifacts.py`)

```python
# Build the inner video config. Explainer and Brief expose visual styles;
# Cinematic and Short use a fixed style (Short ignores any style code server-side).
video_config = [
    source_ids_double,
    language,  # "en"
    instructions,  # Focus/customization prompt
    None,
    format_code,  # 1=EXPLAINER, 2=BRIEF, 3=CINEMATIC, 4=SHORT
    style_code,  # None=CUSTOM, 1=AUTO_SELECT, 2=CLASSIC, 3=WHITEBOARD, ...
]
if video_style == VideoStyle.CUSTOM and style_prompt:
    video_config.append(style_prompt)

params = [
    create_artifact_options,
    notebook_id,
    [
        None,  # [0]
        None,  # [1]
        3,  # [2]: ArtifactTypeCode.VIDEO
        source_ids_triple,  # [3]
        None,  # [4]
        None,  # [5]
        None,  # [6]
        None,  # [7]
        [None, None, video_config],  # [8]
    ],
]
```

Live Web UI capture on 2026-06-17 showed these visual-style radio values:
`AUTO_SELECT=1`, `CUSTOM=0`, `CLASSIC=2`, `WHITEBOARD=3`,
`KAWAII=9`, `ANIME=7`, `WATERCOLOR=6`, `RETRO_PRINT=8`,
`HERITAGE=4`, and `PAPER_CRAFT=5`. Because `CUSTOM=0` is the protobuf
default, the Web UI's JSON array omits that field as `null` and appends the
custom visual-style prompt in the 7th slot:

```python
[
    source_ids_double,
    "en",
    "Focus prompt",
    None,
    2,  # VideoFormat.BRIEF
    None,  # VideoStyle.CUSTOM omitted/defaulted
    "Custom visual style",
]
```

#### Cinematic Video Overview (Type 3, format=3)

**Source:** `_artifacts.py::ArtifactsAPI.generate_cinematic_video()` orchestration;
`_web/artifact/generation.py` RPC dispatch (param builders: `_web/params/artifacts.py`)

Cinematic videos use AI-generated documentary footage (Veo 3) instead of
slide-deck animations. They share the standard video RPC (Type 3) but omit
`style_code` and never accept `style_prompt`. Requires a Google AI Ultra
subscription.

```python
params = [
    create_artifact_options,
    notebook_id,
    [
        None,  # [0]
        None,  # [1]
        3,  # [2]: ArtifactTypeCode.VIDEO
        source_ids_triple,  # [3]
        None,  # [4]
        None,  # [5]
        None,  # [6]
        None,  # [7]
        [
            None,
            None,
            [
                source_ids_double,
                language,  # "en"
                instructions,
                None,
                3,  # VideoFormat.CINEMATIC
            ],
        ],  # [8]
    ],
]
```

#### Report (Type 2)

**Source:** `_artifacts.py::ArtifactsAPI.generate_report()` orchestration;
`_web/artifact/generation.py` RPC dispatch (param builders: `_web/params/artifacts.py`)

```python
params = [
    create_artifact_options,
    notebook_id,
    [
        None,  # [0]
        None,  # [1]
        2,  # [2]: ArtifactTypeCode.REPORT
        source_ids_triple,  # [3]
        None,  # [4]
        None,  # [5]
        None,  # [6]
        [
            None,
            [
                title,  # "Briefing Doc" / "Study Guide" / etc.
                description,  # Short description
                None,
                source_ids_double,
                language,  # "en"
                prompt,  # Detailed generation prompt
                None,
                True,
            ],
        ],  # [7]
    ],
]
```

#### Quiz (Type 4, Variant 2)

**Source:** `_web/params/artifacts.py::build_quiz_artifact_params()`

```python
params = [
    create_artifact_options,
    notebook_id,
    [
        None,  # [0]
        None,  # [1]
        4,  # [2]: ArtifactTypeCode.QUIZ_FLASHCARD
        source_ids_triple,  # [3]
        None,  # [4]
        None,  # [5]
        None,  # [6]
        None,  # [7]
        None,  # [8]
        [
            None,
            [
                2,  # Variant: 2=quiz, 1=flashcards, 4=interactive mind map
                None,
                instructions,
                None,
                None,
                None,
                None,
                [quantity_code, difficulty_code],  # quantity: 1=FEWER, 2=STANDARD, 3=MORE
            ],  # difficulty: 1=EASY, 2=MEDIUM, 3=HARD
        ],  # [9]
    ],
]
```

#### Flashcards (Type 4, Variant 1)

**Source:** `_web/params/artifacts.py::build_flashcards_artifact_params()`

```python
params = [
    create_artifact_options,
    notebook_id,
    [
        None,  # [0]
        None,  # [1]
        4,  # [2]: ArtifactTypeCode.QUIZ_FLASHCARD
        source_ids_triple,  # [3]
        None,  # [4]
        None,  # [5]
        None,  # [6]
        None,  # [7]
        None,  # [8]
        [
            None,
            [
                1,  # Variant: 1=flashcards (vs 2=quiz, 4=interactive mind map)
                None,
                instructions,
                None,
                None,
                None,
                [quantity_code, difficulty_code],  # Same order as quiz (#2116).
            ],  # quantity: 1=FEWER, 2=STANDARD, 3=MORE
        ],  # [9]         # difficulty: 1=EASY, 2=MEDIUM, 3=HARD
    ],
]
```

#### Infographic (Type 7)

**Source:** `_artifacts.py::ArtifactsAPI.generate_infographic()` orchestration;
`_web/artifact/generation.py` RPC dispatch (param builders: `_web/params/artifacts.py`)

```python
# Orientation: 1=LANDSCAPE, 2=PORTRAIT, 3=SQUARE
# Detail:      1=CONCISE, 2=STANDARD, 3=DETAILED
# Style:       InfographicStyle enum (1=AUTO_SELECT, 2=SKETCH_NOTE,
#              3=PROFESSIONAL, 4=BENTO_GRID, 5=EDITORIAL, ...).
#              See _types/enums.py::InfographicStyle for the canonical list
#              (public import: notebooklm.types.InfographicStyle).

params = [
    create_artifact_options,
    notebook_id,
    [
        None,  # [0]
        None,  # [1]
        7,  # [2]: ArtifactTypeCode.INFOGRAPHIC
        source_ids_triple,  # [3]
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,  # [4-13]
        [[instructions, language, None, orientation_code, detail_code, style_code]],  # [14]
    ],
]
```

**Note:** Position [14] wraps the config in a single-element list (`[[...]]`),
not `[None, [...]]`. The 6th tuple element `style_code` was added with the
infographic style preset feature; pass `None` to let the backend auto-select.

#### Slide Deck (Type 8)

**Source:** `_artifacts.py::ArtifactsAPI.generate_slide_deck()` orchestration;
`_web/artifact/generation.py` RPC dispatch (param builders: `_web/params/artifacts.py`)

```python
# Format: 1=DETAILED_DECK, 2=PRESENTER_SLIDES
# Length: 1=DEFAULT, 2=SHORT

params = [
    create_artifact_options,
    notebook_id,
    [
        None,  # [0]
        None,  # [1]
        8,  # [2]: ArtifactTypeCode.SLIDE_DECK
        source_ids_triple,  # [3]
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,  # [4-15]
        [[instructions, language, format_code, length_code]],  # [16]
    ],
]
```

#### Data Table (Type 9)

**Source:** `_artifacts.py::ArtifactsAPI.generate_data_table()` orchestration;
`_web/artifact/generation.py` RPC dispatch (param builders: `_web/params/artifacts.py`)

```python
params = [
    create_artifact_options,
    notebook_id,
    [
        None,  # [0]
        None,  # [1]
        9,  # [2]: ArtifactTypeCode.DATA_TABLE
        source_ids_triple,  # [3]
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,  # [4-17]
        [None, [instructions, language]],  # [18]
    ],
]
```

#### Mind Map (Type 5) - Uses GENERATE_MIND_MAP (yyryJe)

**Source:** `_web/artifact/generation.py::generate_mind_map()`

**Note:** Mind map uses a different RPC method than other artifacts.

```python
# RPC: GENERATE_MIND_MAP (yyryJe), NOT CREATE_ARTIFACT
# Python signature:
#   generate_mind_map(notebook_id, source_ids=None, language="en", instructions=None)
params = [
    source_ids_nested,  # 0: [[[sid]] for sid in source_ids]
    None,  # 1
    None,  # 2
    None,  # 3
    None,  # 4
    [
        "interactive_mindmap",  # 5[0]: command name
        [["[CONTEXT]", instructions or ""]],  # 5[1]: instructions (added in v0.4.0)
        language,  # 5[2]: language code, e.g. "en" (added in v0.4.0)
    ],
    None,  # 6
    [2, None, [1]],  # 7: Fixed config
]
```

#### Interactive Mind Map (Type 4 / variant 4) - Uses CREATE_ARTIFACT (R7cb6c)

**Source:** `_web/params/artifacts.py::build_interactive_mind_map_artifact_params()`,
`_web/mind_maps.py::WebMindMapsAPI.generate()`

NotebookLM's web app now generates an **interactive** mind map — a studio
artifact in the type-4 family with `variant 4` (distinct from the note-backed
JSON mind map above, which the library adapts using the genuine backend mind-map
type code 5).
Unlike the synchronous note-backed kind, this is created asynchronously via
`CREATE_ARTIFACT` and polled to completion (issue #1256).

```python
# RPC: CREATE_ARTIFACT (R7cb6c) — interactive mind map
params = [
    create_artifact_options,
    notebook_id,
    [
        None,
        None,
        4,  # 2: artifact type (type-4 family)
        [[[sid]] for sid in source_ids],  # 3: nested source ids
        None,
        None,
        None,
        None,
        None,
        [None, [4]],  # 9: [_, [variant]] → variant 4 = interactive mind map
    ],
]

# With a custom prompt: variant at [9][1][0], free-text prompt at [9][1][2] —
# the same options-block layout quiz/flashcards use. The server honors it for
# variant 4 (verified live: it steers the generated node tree), and the prompt
# reads back from the LIST_ARTIFACTS [9][1][2] slot that
# ``ArtifactRow.generation_prompt`` decodes.
params[2][9] = [None, [4, None, "focus only on the three astronauts"]]
```

**Reading the tree:** the interactive map exposes its `{"name", "children"}`
node tree through `GET_INTERACTIVE_HTML` (v9rmvd) — the same RPC used for
quiz/flashcard HTML, but the JSON tree lives at **`[0][9][3]`** (the rendered
HTML body is at `[0][9][0]`). `client.mind_maps.get_tree()` and
`download_mind_map` both read that position; `client.mind_maps` unifies the two
kinds behind a single `MindMapKind` discriminator.

### RPC: LIST_ARTIFACTS (gArtLc)

**Source:** `_web/artifacts.py::list()`, `_artifacts.py::poll_status()`

```python
params = [
    [2],
    notebook_id,
    'NOT artifact.status = "ARTIFACT_STATUS_SUGGESTED"',  # Filter string
]

# Live web UI/CDP capture on 2026-06-15 includes projection/type filters
# inside the request-options wrapper:
params = [
    [
        2,
        None,
        None,
        [1, None, None, None, None, None, None, None, None, None, [1]],
        [[1, 4, 8, 2, 3, 6]],
    ],
    notebook_id,
    'NOT artifact.status = "ARTIFACT_STATUS_SUGGESTED"',
]

# Response contains artifacts array with an ArtifactStatus code:
# status = 0 → Unknown
# status = 1 → Pending    (ARTIFACT_STATUS_INITIALIZED — queued, worker not started)
# status = 2 → In progress (ARTIFACT_STATUS_PROCESSING — actively generating)
# status = 3 → Completed  (ARTIFACT_STATUS_READY)
# status = 4 → Failed
# status = 5 → Suggested  (excluded by the filter above)
# status = 6 → ARTIFACT_PENDING_REVIEW (semantics unconfirmed; never observed here)
# Codes 1 and 2 were transposed in this client before #2127.
```

**Quiz/flashcards options are echoed back (#2195).** The server stores the
generation options and returns them on every listing, inside the same type-4
options block that carries the variant and the free-text prompt:

```python
row[9][1][0]  # variant: 1=flashcards, 2=quiz, 4=interactive mind map
row[9][1][2]  # free-text prompt
row[9][1][6]  # flashcards [quantity, difficulty] — null on a quiz row
row[9][1][7]  # quiz       [quantity, difficulty] — null on a flashcards row
```

Positions follow `AppArtifactGenerationOptions` in `docs/android/schema.proto`
(`flashcardsGenerationOptions` = tag 7, `quizGenerationOptions` = tag 8), and
each pair is `[quantity, difficulty]` — the same order both builders send. Read
them through `ArtifactRow.quiz_options` / `ArtifactRow.flashcards_options`, which
return a named `QuizOptionPair` rather than a positional tuple.

This is the **only** client-side check on the option pair that does not depend
on a fixture we wrote ourselves, which is why #2116 (a transposed flashcards
pair) and #2117 (`MORE` aliased to `STANDARD`) both shipped unnoticed. Live
observations worth knowing:

* an omitted option message echoes back as `null`, and a `[0, 0]`
  (proto3 `*_UNSPECIFIED`) pair echoes back as `[]` — both are accepted and
  generate normally, but what the server then chose is not observable; and
* the VCR tier cannot pin any of this: the `freq` matcher compares request
  bodies shape-only, so `[1,3]`, `[3,1]` and `[null,null]` are identical to it.

**Python API Note:** `artifacts.list()` also fetches mind maps from GET_NOTES_AND_MIND_MAPS and includes them as Artifact objects (type=5). This provides a unified list of all AI-generated content. Mind maps with status=2 (deleted) are filtered out — note that this is the *note* row's own status field, unrelated to the `ArtifactStatus` table above.

---

## Notes

### RPC: CREATE_NOTE (CYK0Xb)

**Source:** `_notes.py::create()`

**Note:** Google ignores title/content in CREATE_NOTE. Must call UPDATE_NOTE after to set actual content.

```python
# Creates note with fixed placeholder values
params = [
    notebook_id,  # 0
    "",  # 1: Empty string (ignored)
    [1],  # 2: Fixed flag
    None,  # 3
    "New Note",  # 4: Placeholder title (ignored)
]
# Then call UPDATE_NOTE to set real title/content
```

### RPC: CREATE_NOTE (saved-from-chat variant) (CYK0Xb)

**Source:** `_web/chat.py::save_chat_answer_as_note()` (Web adapter), with the
payload encoded by `_web/params/chat_note.py` — exposed through the shared
`ChatAPI.save_answer_as_note(...)` workflow.

**Note:** This is the same RPC method ID as plain CREATE_NOTE above, but uses a **7-element** params array (vs the 5-element blank-note form) and **mode flag `[2]`** to tell the server the note carries a saved chat answer. The server stores per-citation source-passage metadata so `[N]` markers in the answer render as hover-anchored links in the NotebookLM web UI. No follow-up UPDATE_NOTE is needed — this is a single round-trip.

Reverse-engineered from a captured web-UI "Save to note" request (issue #660). Pinned by fixture and golden unit test at `tests/unit/fixtures/save_chat_as_note_create_note_request.json` and `tests/unit/test_save_chat_as_note_encoder.py::test_golden_single_citation`.

```python
params = [
    notebook_id,  # [0]
    answer_with_markers,  # [1] str — full answer text INCLUDING [N] markers
    [2],  # [2] mode flag — [2] = saved-from-chat (vs [1] = blank-note)
    source_passages,  # [3] list — one descriptor per UNIQUE cited chunk_id
    title,  # [4] str — requested title; server may auto-generate a smart one
    rich_content,  # [5] list — cleaned answer + per-marker anchors (see below)
    [2],  # [6] trailer flag
]
```

`source_passages` (slot `[3]`) — one entry per unique cited chunk:

```python
[
    None,
    None,
    None,
    [[None, source_start, source_end]],  # passage span in source document
    [passage_text_wrapper],  # cited text wrapped with offsets + render flags
    [[[passage_id], source_id]],  # passage_id + source_id pair
    [chunk_id],  # standalone chunk_id
]
```

`rich_content` (slot `[5]`) — five sub-slots:

```python
[
    [
        cleaned_answer_passage_group,      # answer text WITHOUT [N] markers, with offsets
        [                                   # per-marker anchors
            [[chunk_id], [None, 0, position_of_marker_in_clean_text]],
            # ...one entry per [N] in the answer
        ],
    ],
    None,                                   # always-null slot
    None,                                   # always-null slot
    [                                       # source_passages keyed by chunk_id
        [[chunk_id], <same descriptor shape as one entry of slot [3]>],
    ],
    1,                                      # trailer flag
]
```

**Response shape** (6 elements — same shape as a stored note row):

```python
[
    note_id,  # [0] server-assigned UUID
    answer_with_markers,  # [1] echoed
    [2, user_id, [ts_sec, ts_nanos]],  # [2] metadata: type=2 (saved-from-chat)
    source_passages,  # [3] echoed
    server_title,  # [4] may differ from request (smart title)
    rich_content,  # [5] echoed
]
```

**Encoding quirks**:
- Rendering-flag arrays use the integer `0`, not the boolean `false` — `json.dumps(False)` emits `false`, which the server *normalizes* but the wire-channel match is strict. The encoder uses integer `0` to stay byte-exact with the captured request.
- The server appears to apply a "smart title" pass for `[2]`-mode notes — the captured response title differed from the captured request title (the request sent `"New Saved Note"`; the response stored `"Le Verger de la Connaissance : Le Cas de la Pomme"`). `ChatAPI.save_answer_as_note()` surfaces the server-stored title in the returned `Note`.

**Known gaps**:
- The `passage_id` UUID at slot `[3][0][5][0][0]` does NOT appear in the streaming chat response shape we currently parse. `_build_source_passage_descriptor` falls back to `chunk_id` as a placeholder when `ChatReference.passage_id` is unset (which is always, in production today). Empirically the server accepts this and the web UI still renders hover anchors. If a future capture reveals where this UUID comes from, populate `ChatReference.passage_id` in `_web/rows/chat_stream.py::parse_single_citation()` and the encoder will use it automatically.
- Multi-citation segmentation uses a *cumulative-span* heuristic (each `[N]` anchors `clean_text[0..position]` rather than a per-segment span). This matches the captured single-citation payload exactly but is unverified against multi-citation captures. See issue #660 PR description.

### RPC: UPDATE_NOTE (cYAfTb)

**Source:** `_notes.py::update()`

```python
params = [
    notebook_id,  # 0
    note_id,  # 1
    [[[content, title, [], 0]]],  # 2: Triple-nested [content, title, [], 0]
]
```

### RPC: DELETE_NOTE (AH0mwd)

**Source:** `_notes.py::delete()`

**Important:** This is a **soft delete** - it clears note content but does NOT remove the note from the list. The note remains with `None` content and a status flag of `2`.

```python
params = [
    notebook_id,  # 0
    None,  # 1
    [note_id],  # 2: Single-nested note ID
]

# BEFORE delete:
# ['note_id', ['note_id', 'content', [metadata], None, 'title']]

# AFTER delete:
# ['note_id', None, 2]  # Status 2 = deleted/cleared
```

**Note:** Same behavior applies to mind maps via `delete_mind_map()`. The Python API filters out items with status=2 in `list()` and `list_mind_maps()` to match UI behavior.

### RPC: GET_NOTES_AND_MIND_MAPS (cFji9)

**Source:** `_notes.py::_get_all_notes_and_mind_maps()`

```python
params = [notebook_id]

# Live web UI/CDP capture on 2026-06-15:
params = [
    notebook_id,
    None,
    None,  # Refresh calls may send a timestamp here, e.g. [seconds, nanos]
    [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
]
```

---

## Note/Mind Map Data Structures

Notes and mind maps share the same storage system and are distinguished by content format.

### Active Note Structure

```python
[
    "note_id",  # Position 0: Note ID
    [
        "note_id",  # [1][0]: ID (duplicate)
        "content",  # [1][1]: Note content text
        [  # [1][2]: Metadata
            1,  # Type flag
            "user_id",  # User ID
            [ts, ns],  # [timestamp_seconds, nanoseconds]
        ],
        None,  # [1][3]: Unknown
        "title",  # [1][4]: Note title
    ],
]
```

### Active Mind Map Structure

```python
[
    "mind_map_id",  # Position 0: Mind map ID
    [
        "mind_map_id",  # [1][0]: ID (duplicate)
        '{"name": "Root", "children": [...]}',  # [1][1]: JSON with children/nodes
        [metadata],  # [1][2]: Same as notes
        None,  # [1][3]: Unknown
        "Mind Map Title",  # [1][4]: Title
    ],
]
```

### Deleted Item Structure (Status = 2)

```python
["id", None, 2]  # Content cleared, status=2 indicates soft-deleted
```

The Python API:
- `notes.list()` - Returns only active notes (excludes mind maps and status=2)
- `notes.list_mind_maps()` - Returns only active mind maps (excludes status=2)
- `artifacts.list()` - Includes mind maps as Artifact objects (excludes status=2)

---

## Source ID Nesting Patterns

**CRITICAL:** Source IDs require different nesting levels depending on the method.

| Pattern | Structure | Used By |
|---------|-----------|---------|
| Single | `[source_id]` | UPDATE_SOURCE position [1] |
| Double | `[[source_id]]` | Artifact source_ids_double |
| Triple | `[[[source_id]]]` | DELETE_SOURCE, Artifact source_ids_triple |
| Quadruple | `[[[[source_id]]]]` | GET_SOURCE_GUIDE |
| Array of Double | `[[s1], [s2], ...]` | Artifact generation |
| Array of Triple | `[[[s1]], [[s2]], ...]` | Artifact generation |

**Building nesting in Python:**

```python
source_ids = ["source_1", "source_2", "source_3"]

# Single: [source_id]
single = [source_ids[0]]

# Double: [[source_id]]
double = [[source_ids[0]]]

# Triple: [[[source_id]]]
triple = [[[source_ids[0]]]]

# Array of Double for artifacts
source_ids_double = [[sid] for sid in source_ids]
# Result: [["source_1"], ["source_2"], ["source_3"]]

# Array of Triple for artifacts
source_ids_triple = [[[sid]] for sid in source_ids]
# Result: [[["source_1"]], [["source_2"]], [["source_3"]]]
```

---

## Notebook Summary & Sharing

### RPC: SUMMARIZE (VfAZjd)

**Source:** `_web/notebooks.py::WebNotebooksAPI.get_summary()`, `_web/notebooks.py::WebNotebooksAPI.get_description()`

Gets AI-generated summary and suggested topics for a notebook.

```python
params = [
    notebook_id,  # 0: Notebook ID
    [2],  # 1: Fixed flag
]

# Live web UI/CDP capture on 2026-06-15:
params = [
    notebook_id,
    [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
]

# Called with source_path:
await rpc_call(
    RPCMethod.SUMMARIZE,
    params,
    source_path=f"/notebook/{notebook_id}",
)

# Response structure:
# [
#     [                             # [0]: Outer container
#         [summary_text],           # [0][0]: Summary wrapped in list; text at [0][0][0]
#         [[                        # [0][1][0]: Suggested topics array
#             [question, prompt],   # Each topic has question and prompt
#             ...
#         ]],
#         null, null, null,
#         [[question, score], ...], # [0][5]: Topics with relevance scores
#     ]
# ]
```

### RPC: GET_SHARE_STATUS (JFMDGd)

**Source:** `_web/sharing.py::get_status()`

Get the current share settings for a notebook, including users with access and public status.

```python
params = [
    notebook_id,  # 0: Notebook ID
    [2],  # 1: Fixed flag
]

# Live web UI/CDP capture on 2026-06-15:
params = [
    notebook_id,
    [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
]

# Called with source_path:
await rpc_call(
    RPCMethod.GET_SHARE_STATUS,
    params,
    source_path=f"/notebook/{notebook_id}",
)

# Response structure:
# [
#     [  # [0]: List of users with access
#         [
#             "email@example.com",     # [0]: email
#             1,                       # [1]: permission (1=owner, 2=editor, 3=viewer)
#             [],                      # [2]: flags (empty)
#             [
#                 "Display Name",      # [3][0]: display name
#                 "https://..."        # [3][1]: avatar URL
#             ]
#         ],
#         # ... more users
#     ],
#     [true],  # [1]: publicSettings (tag 2) - [isPubliclyReadable, isDiscoverable]
#     1000,    # [2]: maxIndividualsShareLimit (tag 3)
#     true,    # [3]: isPublicSharingAllowed (tag 4)
#     null,    # [4]: null on every row observed
#     null,    # [5]: null on every row observed
#     [3, true, true],  # [6]: tag 7 - UNNAMED, deliberately unread
#     false    # [7]: tag 8 - UNNAMED, deliberately unread
# ]
```

**Response fields (`GetProjectDetailsResponse`).** The shape above is the full
live response, identical on 10/10 notebooks sampled 2026-08. Index 2 was
documented as "unknown constant (ignore)" until #2130 — it is the enforced
collaborator cap.

| Index | Proto tag | Field | Decoded as |
|-------|-----------|-------|------------|
| 0 | 1 | *(shared-user rows)* | `ShareStatus.shared_users` — not declared in the mobile `GetProjectDetailsResponse`, which has no tag 1; see `_wire_contract.py::UNMAPPED` |
| 1 | 2 | `publicSettings` | `ShareStatus.is_public` (via `ProjectPublicSettings.isPubliclyReadable`) |
| 2 | 3 | `maxIndividualsShareLimit` | `ShareStatus.max_individuals_share_limit` |
| 3 | 4 | `isPublicSharingAllowed` | `ShareStatus.is_public_sharing_allowed` |
| 6 | 7 | *(unnamed)* | **unread** — live `[3, true, true]` |
| 7 | 8 | *(unnamed)* | **unread** — live `false` |

> Tags 7 and 8 are populated on every live row but the recovered mobile schema
> declares only tags 2-4, so nothing names them. Exposing them would mean
> inventing a field name, so they are recorded as deliberately-undecoded in
> `tests/_guardrails/_wire_contract.py::UNREAD_SHARE_STATUS_SLOTS` rather than
> surfaced. `test_unread_share_status_slots_stay_undecoded` enforces that record
> — a constant that starts reading slot 6 or 7 fails the guardrail (#2130).
>
> Both decoded fields are **tri-state**: `None` means the response made no claim
> (short responses are real — the pinned golden capture is three elements long)
> and is never collapsed into `0` / `False`. `ProjectPublicSettings.isDiscoverable`
> (tag 2 of the inner block) remains unread.

### RPC: SHARE_NOTEBOOK (QDyure)

**Source:** `_web/sharing.py::set_public()`, `_sharing.py::add_user()`,
`_web/sharing.py::set_users()`, `_web/sharing.py::remove_user()`

Multi-purpose RPC for managing notebook sharing: toggle public access, add/update users, or remove users.

**Toggle public/restricted access:**
```python
# access_value: 0=restricted, 1=anyone with link
params = [
    [
        [
            notebook_id,
            None,  # no user changes
            [access_value],  # [0]=restricted, [1]=public
            [access_value, ""],  # [flag, welcome_message]
        ]
    ],
    1,  # action type
    None,
    [2],  # fixed flag
]

# Response: [] (empty on success)
```

**Add/update users:**
```python
# permission: 2=editor, 3=viewer, 4=remove
# notify_flag: 0=no email, 1=send notification
# message_flag: 0=has message, 1=no message
# entries may mix editor and viewer grants in one request
entries = [
    ["viewer@example.com", None, 3],
    ["editor@example.com", None, 2],
]
params = [
    [
        [
            notebook_id,
            entries,  # users to add/update
            None,  # no public access change
            [message_flag, welcome_message],
        ]
    ],
    notify_flag,  # 0 or 1
    None,
    [2],
]

# Response: [] (empty on success)

# SharingAPI.set_users() sends the SHARE_NOTEBOOK call above once, then calls
# GET_SHARE_STATUS once and returns the refreshed ShareStatus. notify_flag and
# welcome_message apply to every entry in this call. Batching collapses N
# RPC + status-refresh round trips into one; it is NOT established to change
# how many notification emails recipients receive.
```

**Entry-list semantics (live-probed 2026-08-11, two scratch notebooks, `notify=False`).**
The entry list is a general batch **set/upsert** keyed by email, not an add:

| Sent | Observed |
|---|---|
| Two distinct users, mixed VIEWER/EDITOR | Both granted; both grantees could open the notebook |
| Two existing users with flipped permissions | Both permissions changed |
| One existing update batched with one absent add | Both applied |
| Singular update for an absent user | User was added |
| The same email twice, conflicting permissions | RPC returned success, permission **unchanged** — a silent no-op |
| Remove two users, both present | Both removed |
| Remove several where any target is absent | **Whole request silently did nothing**, even with the present user listed first |
| Remove one user and update another in one request | Both applied |

Two consequences the client encodes: `set_users()` rejects duplicate emails before
issuing the RPC (there is no first/last-wins rule to honour), and plural removal is
not offered — it would need a `GET_SHARE_STATUS` preflight, an intersection with the
currently-shared set, and post-verification to avoid the silent all-or-nothing trap.

The duplicate check compares addresses **exactly**, because that is what the probe
covered. Whether two addresses differing only in case resolve to one account is
**not established** — RFC 5321 keeps the local part case-sensitive — so the client
passes them through rather than raising on an unobserved rule. Worth a probe row if
anyone runs this again.

**Remove user:**
```python
params = [
    [
        [
            notebook_id,
            [[email, None, 4]],  # 4 = remove permission
            None,
            [0, ""],
        ]
    ],
    0,  # no notification
    None,
    [2],
]
```

### RPC: SET_VIEW_LEVEL (via RENAME_NOTEBOOK s0tc2d)

**Source:** `_web/sharing.py::set_view_level()`

Set what viewers can access (full notebook vs chat only).

**Note:** This uses the same RPC ID as RENAME_NOTEBOOK (`s0tc2d`) but with different parameter structure.

```python
# view_level: 0=full notebook, 1=chat only
params = [
    notebook_id,  # 0: Notebook ID
    [
        [
            None,
            None,
            None,
            None,  # indices 0-3
            None,
            None,
            None,
            None,  # indices 4-7
            [[view_level]],  # index 8: [[0]] or [[1]]
        ]
    ],
]

# Response: Full notebook data (same as rename response)
```

### Notebook Sharing Overview

Notebook sharing and artifact deep-link sharing are separate toggles:
`SHARE_NOTEBOOK` governs who can open the notebook at all, while
`SHARE_ARTIFACT` is the legacy share-link path used to build or toggle a
notebook URL with an optional `?artifactId=` target.

Notebooks have **three sharing dimensions**:

1. **Notebook visibility** (SHARE_NOTEBOOK - QDyure):
   - `[0]` = Restricted (only explicitly shared users)
   - `[1]` = Anyone with the link

2. **View Level** (RENAME_NOTEBOOK - s0tc2d):
   - `[[0]]` = Full notebook (chat + sources + notes)
   - `[[1]]` = Chat only (viewers can only use chat)

3. **User Permissions** (SHARE_NOTEBOOK - QDyure):
   - `1` = Owner (read-only, cannot be assigned)
   - `2` = Editor (can edit notebook)
   - `3` = Viewer (read-only access)
   - `4` = Remove (internal: remove user from share list)

**Python API:**
```python
# Use client.sharing for all sharing operations
status = await client.sharing.get_status(notebook_id)
await client.sharing.set_public(notebook_id, True)
await client.sharing.set_view_level(notebook_id, ShareViewLevel.CHAT_ONLY)
await client.sharing.add_user(notebook_id, "user@example.com", SharePermission.VIEWER)
await client.sharing.set_users(
    notebook_id,
    [
        ("viewer@example.com", SharePermission.VIEWER),
        ("editor@example.com", SharePermission.EDITOR),
    ],
    notify=True,
    welcome_message="Welcome, team!",
)
```

**Share URLs:**
- Notebook: `https://notebook.google.com/notebook/{notebook_id}`
- Artifact deep-link: `https://notebook.google.com/notebook/{notebook_id}?artifactId={artifact_id}`

The `?artifactId=xxx` parameter creates a deep link that opens the notebook and
navigates to that specific artifact. It does not make the artifact an
independent public resource outside the notebook. Mind Maps cannot be shared
(no public URLs).

---

## Source Refresh Operations

### RPC: REFRESH_SOURCE (FLmJqe)

**Source:** `_web/sources/__init__.py::WebSourcesAPI.refresh()`

Refresh a source to get updated content (for URL/Drive sources).

```python
params = [
    None,  # 0
    [source_id],  # 1: Single-nested source ID
    [2],  # 2: Fixed flag
]

# Called with source_path:
await rpc_call(
    RPCMethod.REFRESH_SOURCE,
    params,
    source_path=f"/notebook/{notebook_id}",
    allow_null=True,  # success is a null payload with nothing at index 5
    raise_on_null_status=True,  # a status-tagged null ([3] live) raises (#2290)
)
```

Success is an empty reply; a rejection arrives as the same null payload tagged
with a gRPC status at index 5 (live 2026-09-01: `[3]` INVALID_ARGUMENT for every
shape tried, on an account where the plural `dtT1F BatchRefreshSources` accepts
the same source id — see #2290 / #2283). `raise_on_null_status=True` keeps the
two apart; without it both decoded to `None`.

### RPC: CHECK_SOURCE_FRESHNESS (yR9Yof)

**Source:** `_web/sources/__init__.py::WebSourcesAPI.check_freshness()`

Check if a source needs to be refreshed.

```python
params = [
    None,  # 0
    [source_id],  # 1: Single-nested source ID
    [2],  # 2: Fixed flag
]

# Called with source_path:
await rpc_call(
    RPCMethod.CHECK_SOURCE_FRESHNESS,
    params,
    source_path=f"/notebook/{notebook_id}",
)

# Response varies by source type:
#   URL sources:   [] (empty array) = fresh
#   Drive sources: [[null, true, [source_id]]] = fresh
#                  [[null, false, [source_id]]] = stale
#   Legacy:        True = fresh, False = stale
```

---

## Research Operations

Research allows searching the web or Google Drive for sources to add to notebooks.

### Source Type Codes

| Code | Source |
|------|--------|
| 1 | Web |
| 2 | Google Drive |

### RPC: DISCOVER_SOURCES (Es3dTe)

**Source:** `_web/research.py::discover()`

Discover web sources for a query in one synchronous round trip (the web UI's
"Discover sources" dialog). Same request message as `START_FAST_RESEARCH`
(`DiscoverSourcesRequest`), a different verb: the call blocks (~8 s live) and
answers with the results directly, and the backend also records it as a
completed job that `POLL_RESEARCH` lists (live-verified on both transports
in issue #2283).

```python
params = [
    [query, 1],  # 0: DiscoveryContext — query text, corpus 1 = Web (Drive fails on this route)
    None,  # 1: RequestContext (optional)
    mode,  # 2: DiscoveryMode — 1 DEFAULT_LLM_SEARCH, 2 RAW_SEARCH, 3 CURIOUS_SEARCH,
    #             4 CURIOUS_RAW_SEARCH (3/4 take an empty query). 5 DEEP_RESEARCH is
    #             rejected (INVALID_ARGUMENT) and 6 LITE_LLM_SEARCH faults (INTERNAL).
    notebook_id,  # 3: Notebook ID (required; unknown id → NOT_FOUND)
]

await rpc_call(
    RPCMethod.DISCOVER_SOURCES,
    params,
    source_path=f"/notebook/{notebook_id}",
)

# Response: [sources, overview, [job_id]]
#
#   sources  — [[url, title, hint, corpus_type], ...]; ten rows on every live
#              call, corpus_type always 1 (150/150 rows), no favicon or deep
#              payload slots. Identical to the [sources, summary] bundle a
#              POLL_RESEARCH task row carries at task_info[3].
#   overview — one sentence summarising the set.
#   job_id   — DiscoverSourcesFeedbackKey[0]: the id of the completed job the
#              call recorded; `discover()` returns it as ``task_id`` so the
#              result imports through IMPORT_RESEARCH like any run.
```

### RPC: START_FAST_RESEARCH (Ljjv0c)

**Source:** `_web/research.py::start()` with `mode="fast"`

Start a fast research session.

```python
# source_type: 1=Web, 2=Drive
params = [
    [query, source_type],  # 0: Query and source type
    None,  # 1
    1,  # 2: DiscoveryMode — 1 = DEFAULT_LLM_SEARCH
    notebook_id,  # 3: Notebook ID
]

# Called with source_path:
await rpc_call(
    RPCMethod.START_FAST_RESEARCH,
    params,
    source_path=f"/notebook/{notebook_id}",
)

# Response: [task_id]
#
# A ONE-element list, in 4/4 captured fast starts. Only deep research returns a
# second slot (2/2 captured deep starts); a fast start never carries a report_id
# (`research_start_fast.yaml` → `['ac0bc757-…']`, `research_start_deep.yaml` →
# `['e9b7cb1c-…', '24f83c74-…']`). `start()` returns both ids as-is; the
# adapters that poll/import/cancel select the mode-specific handle — slot 0 for
# fast, slot 1 for deep (`_app/source_research.py`).
```

### RPC: START_DEEP_RESEARCH (QA9ei)

**Source:** `_web/research.py::start()` with `mode="deep"`

Start the Web batchexecute form of a deep research session. The public Android
research namespace implements its native gRPC counterpart; this payload is only
the Web wire contract.

```python
# This Web RPC's deep-research form supports only source_type=1 (Web research).
params = [
    None,  # 0
    [1],  # 1: Fixed flag
    [query, source_type],  # 2: Query and source type
    5,  # 3: DiscoveryMode — 5 = DEEP_RESEARCH
    notebook_id,  # 4: Notebook ID
]

# Called with source_path:
await rpc_call(
    RPCMethod.START_DEEP_RESEARCH,
    params,
    source_path=f"/notebook/{notebook_id}",
)

# Response: [task_id, report_id]
#
# Exactly two elements in 2/2 captured deep starts. The FIRST is an unpollable
# sessionId; POLL_RESEARCH / IMPORT_RESEARCH / CANCEL_RESEARCH key off the second.
```

Deep research is not complete after `QA9ei` alone. In the observed browser/client
flow, the returned `report_id` later becomes important during polling and import:

1. `QA9ei` starts the deep research job and returns `[task_id, report_id, ...]`
2. `e3bVqc` polls the notebook for all research tasks and exposes the report content
3. `LBwxtb` imports the report entry plus selected web sources using the later
   polled deep-research task ID, which commonly matches the earlier `report_id`

### RPC: POLL_RESEARCH (e3bVqc)

**Source:** `_web/research.py::poll()`

Poll for research results.

```python
params = [
    None,  # 0
    None,  # 1
    notebook_id,  # 2: Notebook ID
]

# Called with source_path:
await rpc_call(
    RPCMethod.POLL_RESEARCH,
    params,
    source_path=f"/notebook/{notebook_id}",
)

# Response structure. The envelope is either [[task, ...]] or a flat [task, ...]
# (`unwrap_poll_tasks` probes both). Slot inventory below is from 9 task rows in
# 10 POLL frames across six cassettes — five carry rows (3 deep / 6 fast), one is
# empty-only:
# [
#     [
#         task_id,              # [0]: str — the poll/import/cancel handle
#         task_info,            # [1]: see below
#         updated_at,           # [2]: [seconds, nanos] — LAST-UPDATE time. Read as
#                               #      ResearchTask.updated_at (#2122).
#         created_at,           # [3]: [seconds, nanos] — CREATION time. Read as
#                               #      ResearchTask.created_at (#2122).
#         account_id,           # [4]: str — the account the run belongs to. Read as
#                               #      ResearchTask.account_id (#2122).
#     ],
#     ...
# ]
#
# task_info (len 5 on fast, 6 on deep):
# [
#     None,                     # [0]
#     query_info,               # [1]: [query_text, source_type]  (1=web, 2=drive)
#     discovery_mode,           # [2]: DiscoveryMode — 1 = DEFAULT_LLM_SEARCH (6/6
#                               #      fast rows), 5 = DEEP_RESEARCH (3/3 deep rows).
#                               #      The same enum the start params carry, so mode
#                               #      is two-sided confirmable. Read as
#                               #      ResearchTask.discovery_mode (#2122).
#     sources_and_summary,      # [3]: [[sources], summary_text] — None until results
#     status_code,              # [4]: see the status-code table below
#     deep_run_block,           # [5]: DEEP ONLY (3/3 deep rows, 0/6 fast):
#                               #      [id, base64_blob | None, int, None, model_tag].
#                               #      [0] is NOT the poll handle: in the one cassette
#                               #      that links a start to its polls, [0] is the deep
#                               #      start's slot-0 id (the unpollable sessionId)
#                               #      while task[0] is slot 1. [1] is None on the
#                               #      first poll, then 888 and 222596 base64 chars as
#                               #      the run progresses. [2] was 1, 1, 5 — not fixed.
#                               #      model_tag was 'deep_research.flash.prod'.
#                               #      All unread.
# ]
#
# sources_and_summary[0] can contain a mix of:
#
# Fast research web source:
# [url, title, desc, type]                       # len 4 in 20/46 captured rows
#
# A deep row is the SAME row type carrying three more populated slots, so a
# reader that stops at [3] silently drops them (46 source rows captured):
#   [2]  DiscoveredSource.hint — the backend's one-line "why this source" note.
#        Live #2122: populated on 10/10 fast rows; the deep report row is null.
#        Read as ResearchSource.hint
#   [4]  unpopulated in every captured row
#   [5]  favicon URL — 25/46 rows, every value a `t*.gstatic.com/faviconV2?…`
#        `type=FAVICON` URL; unread
#   [6]  typed content block — 26/46 rows; see the kind discriminator below
#   [8]  1-based integer source ordinal, 1..24 — 24/46 rows. Whether it equals
#        the report's citation numbering is unverified here (see #2141)
# Row lengths seen: 4 (x20), 7 (x2), 9 (x24).
#
# Deep research report source (captured shape):
# [None, title, None, type, None, None,
#  [report_markdown, 3, None, None, None, structured_document]]
#
# Deep research web source content blocks use kind 1 or 2 and carry their
# snippet at position 2; they are not report rows. In the observed deep payload,
# kind-1 web-source rows carry an integer ordinal at source position 8, a 1-based
# bijection over the task's discovered sources. Whether that ordinal equals the
# report's own citation numbering is NOT established: research_deep_poll_long.yaml
# carries 24 such ordinals against a report containing no [cite: N] markers at all.
#
# Compatibility shape accepted by the parser (not seen in captures):
# [None, [title, report_markdown], None, type, ...]
#
# Notes:
# - The RPC returns all research tasks for the notebook, not just the latest one.
# - The client exposes all parsed tasks via an additive `tasks` field and keeps the
#   top-level return value backward-compatible as the latest task.
# - For deep research, sources parsed from poll() carry `research_task_id`, which is
#   later used by IMPORT_RESEARCH.
```

#### Task-level metadata (`task[2]` / `[3]` / `[4]`, `task_info[2]`) — #2122

These four always-populated slots were decoded in #2122. The two timestamps are
`[seconds, nanos]` pairs; only the seconds are decoded, matching every other
timestamp read in this client.

| Slot | Meaning | Surfaced as |
|------|---------|-------------|
| `task[2]` | last-update time | `ResearchTask.updated_at` |
| `task[3]` | creation time | `ResearchTask.created_at` |
| `task[4]` | owning account id (opaque string) | `ResearchTask.account_id` |
| `task_info[2]` | `DiscoveryMode` the run executes under | `ResearchTask.discovery_mode` |

> **`task[2]` is update and `task[3]` is create** — the reverse of the labels in
> issue #2122. Established the only way that distinguishes them: polling one
> live run twice, 7.6s apart, `[2]` advanced while `[3]` held the value both
> slots shared on the first poll. Reproduced on a second account, and
> corroborated by 9/9 cassette task rows (`[2]` advanced across all 3
> within-cassette repeated-row transitions; `[3]` was constant across all 4).
>
> **`task[4]` is account-scoped**, which the cassettes alone could not show —
> all of them carry the same `400237754469`. A second live account produced
> `838504205497`, each value constant across every task and poll of its account.
> Whether it names the run's *starter* or the notebook's *owner* is **not**
> established: both were the same account in both probes.

#### Task status codes (`task_info[4]`)

Captured live against the serving backend for issue #1964 — except code `6`,
which is inherited from an earlier undocumented claim and has **never** appeared
in a capture (see the note below). `ResearchStatus` coarsens these into `in_progress` /
`completed` / `failed`; `ResearchTask.termination_reason` keeps the distinction
the coarse status loses, and is derived from the same table so the two can never
disagree.

| Code | Meaning | Termination reason | Observed in |
| --- | --- | --- | --- |
| `1` | Run in flight | `in_progress` | Every run, before it settles |
| `2` | Completed with results | `completed` | Fast web, fast Drive, **and deep** runs |
| `3` | **No matches** — terminal, zero sources | `no_results` | Drive runs (only place observed) |
| `4` | Cancelled via `CANCEL_RESEARCH` | `cancelled` | A deep run cancelled mid-flight |
| `6` | Completed (assumed) | `completed` | **Never observed** — see note |

Notes:

- **Code `3` reads as Drive's "found nothing" signal, not an error.** It arrives
  with a sources bundle carrying no sources (`[None, None, None, None, 1]`) and
  zero parsed sources. Reproduced with three distinct non-matching Drive queries;
  not seen on a web run in these probes, where the same gibberish query still
  returned code `2` with loosely-related results. Three captures are strong
  evidence, not proof — the decode only asserts `no_results` inside the envelope
  it was observed in, and falls back to `unknown` for a code-3 row that
  nonetheless carries sources. Treating it as an undifferentiated failure is
  what issue #1964 fixed.
- **A cancelled run is code `4`, distinct from `3`.** Confirmed by cancelling a
  deep run mid-flight. Fast runs finish server-side before a cancel can land, so
  a fast run cancelled immediately after start still completes with code `2`.
- **Code `6` has no captured support, and deep research completes with `2`.**
  Across the repo's own POLL cassettes — 9 task rows in 10 frames, 3 deep and 6
  fast — the only codes present are `1` (6 rows, in flight) and `2` (3 rows,
  completed). All three completed rows carry `2`, including the deep run in
  `research_deep_poll_long.yaml` (`task_info[2] == 5`), which is what refutes the
  old "code 6 = deep completion" attribution. The `6 → completed` coarsening is
  KEPT — an absent observation is not a refutation, and the fallback is free —
  but it is forward-compat, not observed behaviour. The unit fixtures that used
  `6` to stand for "a completed deep run" now use the captured `2`; the two
  dedicated `6` tests (one parser-level, one through `poll()`) say in their names
  that the code is unobserved.
- Any other terminal code maps to the `unknown` reason rather than being guessed
  at — these codes are undocumented Google internals in the same volatility class
  as the RPC method ids.

### RPC: IMPORT_RESEARCH (LBwxtb)

**Source:** `_web/research.py::import_sources()`

Import selected research sources into the notebook.

```python
# Build source array from selected sources
# Deep research imports prepend a special report entry before regular web sources.
#
# NOTE: this is the REQUEST the client sends. It is a different shape from the
# POLL_RESEARCH *response* row documented above — the report body rides at index 1
# here, whereas a response row carries it in the src[6] kind-3 content block. Built
# by `_web/research.py::_build_report_import_entry` / `_build_web_import_entry`.
source_array = []

# Deep research report entry (outgoing import request):
source_array.append(
    [
        None,  # 0
        [title, markdown],  # 1: Report title and full markdown body
        None,  # 2
        3,  # 3: Special report marker
        None,  # 4
        None,  # 5
        None,  # 6
        None,  # 7
        None,  # 8
        None,  # 9
        3,  # 10: Special report marker
    ]
)

# Standard web source entry:
source_array.append(
    [
        None,  # 0
        None,  # 1
        [url, title],  # 2: URL and title
        None,  # 3
        None,  # 4
        None,  # 5
        None,  # 6
        None,  # 7
        None,  # 8
        None,  # 9
        2,  # 10: Standard web-source marker
    ]
)

params = [
    None,  # 0
    [1],  # 1: Fixed flag
    task_id,  # 2: Research task ID (for deep research, use the polled task ID)
    notebook_id,  # 3: Notebook ID
    source_array,  # 4: Array of sources to import
]

# Called with source_path:
await rpc_call(
    RPCMethod.IMPORT_RESEARCH,
    params,
    source_path=f"/notebook/{notebook_id}",
)

# Response: Imported notebook sources with IDs
#
# Notes:
# - Deep research report preservation depends on importing the special report entry,
#   not just the URL sources.
# - The browser/client flow uses the later polled deep-research task ID here rather
#   than blindly reusing the original task ID returned by START_DEEP_RESEARCH.
# - This call commonly runs long on large batches (the server fetches/parses/
#   embeds every entry before responding), so the client sends a batch-scaled
#   `read_timeout` here rather than the shared 30s default — see
#   `_web/research_import.py::_import_research_read_timeout` (#2187).
# - A client-side timeout can still land AFTER the server partially commits.
#   Retrying with the same task_id then gets rejected with gRPC 9
#   (FAILED_PRECONDITION) — documented backend behavior (#1926 item F2b), not
#   a novel failure. `import_sources_with_verification` re-probes
#   `sources.list` on this error: if it verifies every requested URL already
#   landed, that's treated as success; otherwise the error surfaces
#   immediately rather than retrying blindly against the same rejected
#   task_id (unlike a timeout, this attempt's payload was rejected outright,
#   so a filtered-subset retry isn't evidence-based) — see
#   `_web/research_import.py::_is_import_research_failed_precondition`.
```

### RPC: CANCEL_RESEARCH (Zbrupe)

**Source:** `_web/research.py::ResearchAPI.cancel()`

Cancel an in-flight research (DiscoverSources) run. An IN_PROGRESS run
transitions to a terminal `FAILED` state shortly after this call; cancelling an
already-terminal run is a silent no-op.

```python
params = [
    None,  # 0: optional client context — omitted (matches start/poll)
    None,  # 1
    run_id,  # 2: the poll-level run id (== ResearchTask.task_id from poll())
]

await rpc_call(
    RPCMethod.CANCEL_RESEARCH,
    params,
    source_path=f"/notebook/{notebook_id}",
)

# Response: [] unconditionally.
#
# Notes (all LIVE-VERIFIED end-to-end against a scratch notebook):
# - Fire-and-forget. The server returns an empty payload and does NOT validate
#   the run id (a garbage all-zeros id also returns []), so the response carries
#   no success signal. ``cancel()`` returns None and never raises on an unknown
#   id. Confirm by polling afterward — a cancelled IN_PROGRESS run reads FAILED
#   within a few seconds; re-cancelling an already-terminal run is a silent no-op.
# - run_id is the poll-level id. For DEEP research that is the report_id from
#   START_DEEP_RESEARCH: deep's task_id is a sessionId that POLL_RESEARCH reports
#   as NOT_FOUND, and cancelling with it is a SILENT NO-OP (run keeps running) —
#   only the report_id stops a deep run. For FAST research it is the task_id
#   (fast returns no report_id). poll().task_id is the safe value for both modes.
# - notebook_id (source-path) is ROUTING ONLY, not a scoping/authorization
#   boundary: a valid run_id is cancelled even when source-path names a different
#   / non-existent / empty notebook. The run id alone identifies the run server-side.
```

---

## User Settings

Global user settings that affect all notebooks in an account.

### RPC: GET_USER_SETTINGS (ZwVcOc)

**Source:** `_web/settings.py::get_output_language()`

Get user settings including the current output language.

```python
params = [
    None,  # 0
    [1, None, None, None, None, None, None, None, None, None, [1]],  # 1: Fixed config
]

# Live web UI/CDP capture on 2026-06-15 from notebook context:
params = [
    [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
]

# Called with root source_path:
await rpc_call(
    RPCMethod.GET_USER_SETTINGS,
    params,
    source_path="/",  # Global setting uses root path
)

# Response structure:
# [[
#     null,
#     [6, 500, 300, 500000, 2],     # [0][1]: Limits/quotas (captured 2026-07-11)
#     [true, null, null, true, ["ja"]],  # [0][2]: Settings (language at [4][0])
#     [[1]],                         # [0][3]: Unknown
#     [true, 1, 3, 2]               # [0][4]: Feature flags
# ]]
#
# Historical: the limits block was captured 4-element as [6, 500, 300, 500000] on
# 2026-06-15 (pre-tier). Google appended index 4 (tier) after that; confirmed live
# 2026-07-11 across a Free profile (…, 1) and two Pro profiles (…, 2).
#
# Language code at: result[0][2][4][0]
# Notebook limit at: result[0][1][1]
# Source limit at: result[0][1][2]
# Max characters per source at: result[0][1][3]  (e.g. 500000)
# Tier enum at: result[0][1][4]  — OPAQUE key, not an ordinal rank.
#   1=Standard/Free, 2=Pro, 4=Plus, 3=Ultra(20TB), 6=Ultra(30TB); 5=Expanded (aligns
#   with the Workspace "Expanded" access level, not a consumer plan). Live-confirmed
#   1 & 2 (source limits match Google's published 50 / 300). Full per-tier limits:
#   docs/quota-limits.md
```

The full per-tier notebook/source/studio limits these enum values map to are documented in
[quota-limits.md](quota-limits.md).

### RPC: SET_USER_SETTINGS (hT54vc)

**Source:** `_web/settings.py::set_output_language()`

Set user settings (currently used for output language).

**Important:** This is a **GLOBAL setting** that affects all notebooks in the account.

```python
# Language code goes in a triple-nested structure
params = [
    [[None, [[None, None, None, None, [language]]]]],  # 0: Nested language config
]

# Called with root source_path:
await rpc_call(
    RPCMethod.SET_USER_SETTINGS,
    params,
    source_path="/",  # Global setting uses root path
)

# Response structure:
# [
#     null,
#     [6, 500, 300, 500000],              # [1]: Limits
#     [true, null, null, true, ["ja"]],   # [2]: Updated settings (language at [4][0])
#     ...
# ]
#
# Language code at: result[2][4][0]
```

**Supported Languages:**

Common language codes include:
- `en` (English), `ja` (日本語), `zh_Hans` (中文简体), `zh_Hant` (中文繁體)
- `ko` (한국어), `es` (Español), `fr` (Français), `de` (Deutsch), `pt_BR` (Português)
- See `_app/language.py::SUPPORTED_LANGUAGES` for the full list of 80+ languages

---

## Artifact Management

### RPC: RENAME_ARTIFACT (rc3d8d)

**Source:** `_web/artifacts.py::rename()`

Rename an artifact.

```python
params = [
    [artifact_id, new_title],  # 0: Artifact ID and new title
    [["title"]],  # 1: Field mask (update title)
]

# Called with source_path:
await rpc_call(
    RPCMethod.RENAME_ARTIFACT,
    params,
    source_path=f"/notebook/{notebook_id}",
)
```

### RPC: EXPORT_ARTIFACT (Krh3pd)

**Source:** `_web/artifacts.py::export_report()`, `_web/artifacts.py::export_data_table()`, `_web/artifacts.py::export()`

Export an artifact to Google Docs or Sheets.

```python
# Export types:
# 1 = Google Docs
# 2 = Google Sheets

params = [
    None,  # 0
    artifact_id,  # 1: Artifact ID
    content,  # 2: Content to export (optional, can be None)
    title,  # 3: Title for exported document
    export_type,  # 4: 1=Docs, 2=Sheets
]

# Called with source_path:
await rpc_call(
    RPCMethod.EXPORT_ARTIFACT,
    params,
    source_path=f"/notebook/{notebook_id}",
)

# Response: Export result with document URL
```

### RPC: REVISE_SLIDE (KmcKPe)

**Source:** `_web/artifacts.py::revise_slide()`,
`_web/artifact/generation.py::ArtifactGenerationService.revise_slide()`,
`_web/params/artifacts.py::build_revise_slide_params()`

Revise one slide in an existing completed slide deck. `slide_index` is
zero-based and must be non-negative.

```python
params = [
    [2],  # 0: Fixed flag
    artifact_id,  # 1: Slide deck artifact ID
    [[[slide_index, prompt]]],  # 2: Revision request
]

# Called with source_path:
await rpc_call(
    RPCMethod.REVISE_SLIDE,
    params,
    source_path=f"/notebook/{notebook_id}",
    allow_null=True,
)
```

Contract (ADR-0019 "async kickoff"): an accepted revision returns a standard
`GenerationStatus` parsed from the RPC response; a synchronous server refusal
propagates as `RateLimitError` / `RPCError`; a null result raises
`ArtifactFeatureUnavailableError`.

### RPC: RETRY_ARTIFACT (Rytqqe)

**Source:** `_web/artifacts.py::retry_failed()`

Retry a failed Studio artifact in place — the equivalent of the NotebookLM web
UI "Retry" button. The failed artifact is **not** deleted first; the same
`artifact_id` is preserved and the artifact moves from `failed` back to
`pending` (re-queued), so existing `poll_status()` / `wait_for_completion()`
flows keep working against it. Captured/validated across video, audio, and
infographic artifacts (issue #1319).

```python
params = [
    retry_options,  # 0: fixed client capability blob (see below)
    artifact_id,  # 1: ID of the failed artifact to retry
]

# retry_options is a type-agnostic literal, sent verbatim regardless of
# artifact type. The trailing [[1, 4, 8, 2, 3, 6]] is a static
# artifact-type-code capability list, not artifact-specific.
retry_options = [
    2,
    None,
    None,
    [1, None, None, None, None, None, None, None, None, None, [1]],
    [[1, 4, 8, 2, 3, 6]],
]

# Called with source_path:
await rpc_call(
    RPCMethod.RETRY_ARTIFACT,
    params,
    source_path=f"/notebook/{notebook_id}",
    allow_null=True,
)
```

**Response:** payload index `0` is a standard artifact row (positionally
identical to a `LIST_ARTIFACTS` row): `row[0]` is the same `artifact_id`
(returned as the task id) and `row[4] == 1` (`ARTIFACT_STATUS_INITIALIZED` →
`"pending"`; this was mislabelled `PROCESSING`/`in_progress` before #2127).

Contract (ADR-0019 "async kickoff"): an accepted retry returns
`GenerationStatus(status="pending")`; a synchronous server refusal
(`USER_DISPLAYABLE_ERROR` — rate limit, quota, or non-retryable artifact)
**raises** the underlying `RateLimitError` / `RPCError`; a null / missing-id
result raises `ArtifactFeatureUnavailableError`. A retry may still fail again
provider-side — observed by polling as a later terminal `failed` status — so
callers decide whether to re-invoke.

### RPC: SHARE_ARTIFACT (RGP97b)

**Source:** `_web/sharing.py::ShareManager.share()` (legacy share-link toggle)

Toggle the legacy share-link state for a notebook URL, optionally with an
artifact deep-link target. Distinct from `SHARE_NOTEBOOK` (`QDyure`), which
governs notebook visibility and user permissions.

Note: Mind Maps are NOT shareable (they don't have public URLs).

```python
# share_options: [1] for public, [0] for private
# Payload shape is conditional on artifact_id:
#   - Without artifact_id: 2-tuple [share_options, notebook_id]
#   - With artifact_id (truthy): 3-tuple [share_options, notebook_id, artifact_id]
# Sending a third positional element when artifact_id is None/empty changes the
# wire payload, so callers MUST omit it rather than pass null.
share_options = [1] if public else [0]
if artifact_id:
    params = [share_options, notebook_id, artifact_id]
else:
    params = [share_options, notebook_id]

# Called with source_path:
await rpc_call(
    RPCMethod.SHARE_ARTIFACT,
    params,
    source_path=f"/notebook/{notebook_id}",
)

# Share URL format:
# - Notebook: https://notebook.google.com/notebook/{notebook_id}
# - Artifact deep-link: https://notebook.google.com/notebook/{notebook_id}?artifactId={artifact_id}
```

**Important:** The `?artifactId=xxx` URL is a **deep link** - it opens the shared notebook and navigates to that artifact. The artifact itself isn't independently shared.

### RPC: GET_INTERACTIVE_HTML (v9rmvd)

**Source:** `_web/artifact/downloads.py::_get_artifact_content()` (quiz/flashcard HTML), `_web/artifact/downloads.py::_get_interactive_mind_map_tree()` (interactive mind-map tree)

Fetch the interactive payload for a studio artifact. Used both for quiz/flashcard
HTML and for the **interactive** mind-map JSON node tree (issue #1256) — the same
RPC, but the two kinds read different cells of index `9`.

```python
params = [artifact_id]  # Just the artifact ID

# Called with source_path:
await rpc_call(
    RPCMethod.GET_INTERACTIVE_HTML,
    params,
    source_path=f"/notebook/{notebook_id}",
)

# Response structure:
# [[
#     ...,                    # indices 0-8: metadata
#     [                       # index 9: interactive content array
#         html_content,       #   [9][0]: rendered HTML body (quiz / flashcard)
#         ...,
#         ...,
#         tree_json,          #   [9][3]: interactive mind-map {"name","children"} tree (JSON string)
#     ],
#     ...
# ]]
#
# Quiz/flashcard download reads [0][9][0] (HTML → JSON/Markdown/HTML).
# Interactive mind-map download reads [0][9][3] (the JSON node tree).
```

### RPC: GET_SUGGESTED_REPORTS (ciyUvf)

**Source:** `_web/artifacts.py::suggest_reports()`

Get AI-suggested report formats based on notebook content.

```python
params = [
    [2],  # 0: Fixed flag (same pattern as LIST_ARTIFACTS)
    notebook_id,  # 1: Notebook ID
]

# Called with source_path:
await rpc_call(
    RPCMethod.GET_SUGGESTED_REPORTS,
    params,
    source_path=f"/notebook/{notebook_id}",
)

# Response structure:
# [[
#     [title, description, None, None, prompt, audience_level],
#     ...
# ]]
#
# Example response item:
# ["Research Paper", "An academic paper analyzing...", None, None,
#  "Write a research paper for an academic audience...", 2]
#
# audience_level: 1=Beginner, 2=Intermediate, 3=Advanced
```

**Note:** This is the dedicated RPC method for getting suggested report formats. Previously `ACT_ON_SOURCES` with `"suggested_report_formats"` command was attempted but it doesn't work correctly.

---

## Rejection Frames: `google.rpc.Status` at `wrb.fr` index 5

**Verified 2026-08-13** (live probe + a sweep of all 141 cassettes).

When a `batchexecute` RPC is rejected, the server answers with a `wrb.fr` frame
whose *result* slot (index 2) is `null` and whose index 5 carries a
JSON-array-encoded [`google.rpc.Status`](https://github.com/googleapis/googleapis/blob/master/google/rpc/status.proto).
That is a **public** Google type, not a Tailwind message, so it is absent from
`docs/android/schema.proto` and its positions are the proto tags minus one:

| Index | `google.rpc.Status` field | Observed |
|-------|---------------------------|----------|
| 0 | `code` (tag 1) | Yes — see the table below |
| 1 | `message` (tag 2) | **Never populated.** See "The reason gap" |
| 2 | `details` (tag 3) | Yes — the `UserDisplayableError` block |

Observed codes:

| Payload | RPC | Where |
|---------|-----|-------|
| `[3]` INVALID_ARGUMENT | `CREATE_ARTIFACT` (`R7cb6c`) | Live 2026-08-13: audio overview on a source-less notebook |
| `[3]` INVALID_ARGUMENT | streamed chat | `tests/cassettes/web/chat_ask_oversized_rejection.yaml` (#1472) |
| `[3]` INVALID_ARGUMENT | `SHARE_NOTEBOOK` (`QDyure`) | `tests/cassettes/web/cli_share_add.yaml`, `cli_share_remove.yaml` — swallowed; the flow reports success |
| `[3]` INVALID_ARGUMENT | `SHARE_ARTIFACT` (`RGP97b`) | `tests/cassettes/web/notebooks_share.yaml` — swallowed; the flow reports success |
| `[5]` NOT_FOUND | `CREATE_ARTIFACT`, `RETRY_ARTIFACT`, `REVISE_SLIDE`, `GET_NOTEBOOK` | Live 2026-08-13 (unknown notebook / artifact id); #114 / #294 |
| `[13]` INTERNAL | `REMOVE_RECENTLY_VIEWED` (`fejl7e`) | `tests/cassettes/web/notebooks_remove_from_recent.yaml` — treated as a **successful** no-op |
| `[8, null, [[…UserDisplayableError…]]]` | any | The rate-limit / quota shape |

A sweep of all 141 cassettes found 397 `wrb.fr` frames, only 5 of them
null-result — and all 5 carried one of the shapes above. Four are `batchexecute`
RPCs, across three method ids (`SHARE_NOTEBOOK` ×2, `SHARE_ARTIFACT`,
`REMOVE_RECENTLY_VIEWED`); the fifth is the streamed-chat `[3]` from #1472,
which carries no rpc id and is decoded by `_web/rows/chat_stream.py`, not
`decode_response`.

**Open question.** Only `REMOVE_RECENTLY_VIEWED`'s tolerance has ever been
reasoned about (a cosmetic no-op). Whether the two share rejections are benign
or a refusal being reported as a successful share is unresolved and is *not*
answered here.

Byte-count framing note: the live `CREATE_ARTIFACT` rejection bodies declare
chunk lengths two higher than the chunks actually are (`104` for 102 chars,
`25` for 23), consistently across independent captures. `parse_chunked_response`
is deliberately tolerant of that and counts it via `byte_count_mismatch_total`.

### The reason gap

`google.rpc.Status.message` is the one slot in this envelope where the *server*
could state a human-readable reason. **No captured frame has ever populated
it**: the bare rejections are length-1 arrays, and the recorded
`UserDisplayableError` sample holds `null` there with an int-only detail body.

So every rejection sentence this client prints today is **client-authored** —
including "API rate limit or quota exceeded. Please wait before retrying.",
which is a client guess at what a `UserDisplayableError` means, not something
the server said. The decoder reads the `message` slot defensively (a non-empty
string only) and leads with it when one ever arrives, but until then the guess
is the ceiling. Do not describe this path as "carrying the server's error text"
(#2188).

### `allow_null` and status-tagged nulls

`allow_null=True` means "an empty payload is an acceptable outcome". It used to
also swallow a null the server had *tagged with a rejection*, which is how
`generate_audio` came to report "Audio generation is unavailable" for a live
INVALID_ARGUMENT. Callers that want the server's status instead pass
`raise_on_null_status=True` (`CREATE_ARTIFACT`, `RETRY_ARTIFACT`,
`REVISE_SLIDE` do — all three live-verified above). It is opt-in rather than
blanket because three *other* RPCs are recorded answering a status on flows
this client reports as successful (the table above); flipping them all at once
would change behaviour nobody has evidence about. A swallowed status now logs
at DEBUG, so the remaining cases are findable.

### Artifact failures have no reason at all

`Artifact` in `docs/android/schema.proto` has **no error or failure field**. An
artifact accepted at create time that later transitions to
`ARTIFACT_STATUS_FAILED` therefore carries nothing to explain itself: no cassette
contains a status-4 row, and a live sweep of 27 notebooks / 99 rows found index 3
holding `sources` and index 5 holding `null` on both real FAILED artifacts. The
existence of `RETRY_ARTIFACT` (`Rytqqe`) is consistent with the backend not
persisting a reason — retry is offered because the resource remembers nothing.
Downstream, `_app/generate_retry.py` falls back to a generic
`"{Type} generation failed"`, which is the honest ceiling for that path.

---

## Operation Timing Categories

### Quick Operations

Most operations complete nearly instantly:
- Notebook operations: list, create, rename, delete
- Source metadata: list, rename, delete
- Note operations: create, update, delete
- Chat configuration
- Artifact listing

### Processing Operations

These require backend processing - wait for completion:
- **Add source (URL)**: Network fetch + text extraction
- **Add source (file)**: Upload + parsing
- **Add source (YouTube)**: Transcript extraction
- **Mind Map generation**: Usually faster than other generation types

### Generation Operations

AI-generated content takes significant time:
- **Audio Overview**: Several minutes
- **Video Overview**: Several minutes (longer than audio)
- **Reports/Study Guides**: 1-2 minutes
- **Quiz/Flashcards**: 1-2 minutes
- **Infographic/Slide Deck/Data Table**: 1-2 minutes

### Long-Running Operations

Some operations can run much longer:
- **Deep Research**: Can take many minutes depending on query complexity

### Implementation Note

When automating, poll for completion rather than using fixed timeouts. Check artifact status or source processing state periodically.

---

## Legacy/Unused RPC Methods

These RPC method IDs exist in `rpc/types.py` but are either legacy (superseded by other methods) or not currently used in the implementation. Documented here for completeness.

| RPC ID | Method | Status | Notes |
|--------|--------|--------|-------|

> **Note:** `GET_SOURCE` (`hizoJc`) was previously listed here as "Broken" but is now active — used by `_web/sources/content.py::get_fulltext()`. See [RPC Method Status](#rpc-method-status) and the detailed section above.

**Why keep these?** These IDs are preserved in the codebase in case:
1. Google re-enables or changes their functionality
2. Future reverse-engineering reveals their purpose
3. They become useful for specific edge cases

**Note:** The unified `CREATE_ARTIFACT` (R7cb6c) method handles all artifact generation (audio, video, reports, quizzes, etc.).

## Transfer & Suggestion RPCs (#2283)

Six RPCs the web bundle registers but the web UI never calls from a visible
control. Their request shapes were recovered with the mobile tag oracle
(`docs/android/copy-append-suggestion-evidence.md`): the same
`LabsTailwindOrchestrationService` serves both front doors, so web index `i` is
proto tag `i+1`. All six are also served natively to the Android backend.

### RPC: ADD_SOURCES_ASYNC (X1snv)

**Source:** `_web/sources/transfers.py::SourceTransferService.add_urls_async()` (`WebSourcesAPI.add_urls_async`)

Same request as the batch `ADD_SOURCE` (`AddSources`), but the server answers as
soon as the sources are queued (~0.65 s for two URLs vs ~2 s per synchronous
add in the #2283 web probe) with stub rows.

```python
params = [
    [<source spec>, ...],   # 0: repeated UserContent — the same URL / YouTube specs ADD_SOURCE sends
    notebook_id,            # 1
    [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],  # 2: RequestContext
]
# source_path = f"/notebook/{notebook_id}"
```

**Response:** `[[Source, ...], null, [[Source, ack], ...]]` — stub `Source`
rows at `[0]` (`[[id], url, [null, null, null, null, type]]`; no word count or
ingest timestamps), and a per-source acknowledgement list at `[2]` pairing each
row with an `int` status (`0` on every live observation). Decoded via
`AddSourcesAsyncResponseRow`; never replayed on a transport failure.

### RPC: APPEND_SOURCE (QsNTEd)

**Source:** `_web/sources/transfers.py::SourceTransferService.append_text()` (`WebSourcesAPI.append_text`)

```python
# AppendSourceRequest { SourceId source_id = 2; SourceContent content = 4 }
# SourceContent { PlainTextSourceContent plain_text = 2 }  ->  { header = 1; body = 2 }
params = [
    None,  # 0: unused
    [source_id],  # 1: SourceId
    None,  # 2: unused
    [
        None,
        [header, body],
    ],  # 3: SourceContent.plain_text — doubly nested: a bare string here draws
    #    INVALID_ARGUMENT, a string in the plain_text slot draws INTERNAL
]
```

**Response:** empty on success (`body` lands at the very end of the fulltext;
`header` does not appear in it). Called with `allow_null=True,
raise_on_null_status=True` so a rejected call is not read as that empty success.

### RPC: COPY_SOURCES (R27wvc)

**Source:** `_web/sources/transfers.py::SourceTransferService.copy()` (`WebSourcesAPI.copy`)

Live method `CopySourcesAsync`. The synchronous twin `CopySources` (`Z8UXi`) is
dead on both front doors and is not modelled.

```python
# CopySourcesAsyncRequest { repeated SourceId source_ids = 3; string target_project_id = 4 }
params = [None, None, [[source_id], ...], target_notebook_id]
```

**Response:** `[[ [[original_id], <source entry>], ... ]]` — one mapping entry
per copied source; the entry row is the standard `[[id], title, metadata,
settings]` shape (`CopiedSourceRow`). An unknown source id or target notebook
draws `NOT_FOUND` (live-verified); an empty mapping on success is treated as
`SourceNotFoundError` so a silent no-op never reads as a copy.

### RPC: COPY_ARTIFACTS (mKDdke)

**Source:** `_web/artifacts.py::WebArtifactsAPI.copy()`

Live method `CopyArtifactsAsync`. The synchronous twin `CopyArtifacts`
(`zVGIdd`) validates arity, ignores the ids and copies nothing while reporting
success — never model it.

```python
# CopyArtifactsAsyncRequest { RequestContext = 1; repeated string artifact_ids = 2; string target_project_id = 3 }
params = [
    [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
    [artifact_id, ...],  # bare strings, not [id] wrappers
    target_notebook_id,
]
```

**Response:** `[[ [original_id, <artifact row>], ... ]]` — the full new artifact
row inline (`CopiedArtifactRow` → `Artifact.from_api_response`). Verified live by
re-listing the target.

### RPC: SUGGEST_NEXT_STEPS (OcvKNc)

**Source:** `_web/notebooks.py::WebNotebooksAPI.suggest_next_steps()`

Live method `NextStepSuggestions` — the standalone form of the block every chat
answer carries at `inner[5]`, so no prior conversation is needed.

```python
# NextStepSuggestionsRequest { string project_id = 2; repeated InputSource sources = 3 }
params = [None, notebook_id]  # all sources
params = [None, notebook_id, [[[source_id]], ...]]  # scoped (nest_source_ids depth 2)
```

**Response:** `[[ [question, MagicArtifactType], ... ]]` — three questions per
call live, each with type `9` (`CONVERSATIONAL_TEXT_CHIP`). Decoded through the
chat `NextStepSuggestionRow`. A bogus notebook draws `NOT_FOUND`; a bare
`[id]` at index 2 draws `INVALID_ARGUMENT`.

### RPC: GET_CUSTOMIZATION_CHOICES (sqTeoe)

**Source:** `_web/artifacts.py::WebArtifactsAPI.get_customization_choices()`

Live method `GetArtifactCustomizationChoices`. **Account-level:** `[]`, a bogus
notebook id and every artifact type return the same ~3.3 KB table on both front
doors, so only the request context is sent (the notebook id is appended when
the caller has one; it only fills the ``project_id`` slot). Called with `allow_null=False`: the
server always serves the table, so a null or re-shaped envelope is drift
(`DecodingError`), never "no choices".

```python
params = [[2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]]]
params = [<context>, notebook_id]   # when a notebook id is supplied
```

**Response:** `[[ <audio>, <video>, <slide-deck>, <report presets> ]]` — one
`ArtifactCustomizationChoices` message whose four one-field families each hold
`[[row, ...]]`. Format rows are `[code, title, description]` (codes use
`AudioFormat` / `VideoFormat` / `SlideDeckFormat` values), but this account/UI
availability table is not an exhaustive enum manifest: dedicated options such
as cinematic video may be omitted. Report rows are
`[report_type, description, directive]`. The audio and video families (tags 1–2)
are live-only — the APK schema declares only slides (3) and reports (4).
Decoded via `_web/rows/customization.py`.
