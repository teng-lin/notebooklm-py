# RPC & UI Reference

**Status:** Active
**Last Updated:** 2026-01-18
**Source of Truth:** `src/notebooklm/rpc/types.py`
**Purpose:** Complete reference for RPC methods, UI selectors, and payload structures

> **Note:** Payload structures extracted from actual implementation in `src/notebooklm/`.
> Each payload includes a reference to its source file.

---

## Quick Reference

### RPC Method Status

| RPC ID | Method | Purpose | Implementation |
|--------|--------|---------|----------------|
| `wXbhsf` | LIST_NOTEBOOKS | List all notebooks | `_notebooks.py` |
| `CCqFvf` | CREATE_NOTEBOOK | Create new notebook | `_notebooks.py` |
| `rLM1Ne` | GET_NOTEBOOK | Get notebook details + sources | `_notebooks.py` |
| `s0tc2d` | RENAME_NOTEBOOK | Rename, chat config, share access | `_notebooks.py`, `_chat.py` |
| `WWINqb` | DELETE_NOTEBOOK | Delete a notebook | `_notebooks.py` |
| `izAoDd` | ADD_SOURCE | Add URL/text/YouTube source | `_sources.py` |
| `o4cbdc` | ADD_SOURCE_FILE | Register uploaded file | `_sources.py` |
| `tGMBJ` | DELETE_SOURCE | Delete a source | `_sources.py` |
| `b7Wfje` | UPDATE_SOURCE | Rename source | `_sources.py` |
| `tr032e` | GET_SOURCE_GUIDE | Get source summary | `_sources.py` |
| `R7cb6c` | CREATE_VIDEO | Unified artifact generation | `_artifacts.py` |
| `gArtLc` | LIST_ARTIFACTS | List/poll artifacts | `_artifacts.py` |
| `V5N4be` | DELETE_STUDIO | Delete studio artifact | `_artifacts.py` |
| `hPTbtc` | GET_CONVERSATION_HISTORY | Get chat history | `_chat.py` |
| `CYK0Xb` | CREATE_NOTE | Create a note (placeholder) | `_notes.py` |
| `cYAfTb` | UPDATE_NOTE | Update note content/title | `_notes.py` |
| `AH0mwd` | DELETE_NOTE | Delete a note | `_notes.py` |
| `cFji9` | GET_NOTES_AND_MIND_MAPS | List notes and mind maps | `_notes.py` |
| `yyryJe` | ACT_ON_SOURCES | Mind map generation | `_artifacts.py` |
| `VfAZjd` | SUMMARIZE | Get notebook summary | `_notebooks.py` |
| `FLmJqe` | REFRESH_SOURCE | Refresh URL/Drive source | `_sources.py` |
| `yR9Yof` | CHECK_SOURCE_FRESHNESS | Check if source needs refresh | `_sources.py` |
| `Ljjv0c` | START_FAST_RESEARCH | Start fast research | `_research.py` |
| `QA9ei` | START_DEEP_RESEARCH | Start deep research | `_research.py` |
| `e3bVqc` | POLL_RESEARCH | Poll research status | `_research.py` |
| `LBwxtb` | IMPORT_RESEARCH | Import research results | `_research.py` |
| `rc3d8d` | RENAME_ARTIFACT | Rename artifact | `_artifacts.py` |
| `Krh3pd` | EXPORT_ARTIFACT | Export to Docs/Sheets | `_artifacts.py` |
| `RGP97b` | SHARE_ARTIFACT | Toggle notebook sharing | `_notebooks.py` |
| `QDyure` | SHARE_NOTEBOOK | Set notebook visibility (restricted/public) | `_notebooks.py` |
| `JFMDGd` | GET_SHARE_STATUS | Get notebook share settings | Not implemented |
| `ciyUvf` | GET_SUGGESTED_REPORTS | Get AI-suggested report formats | `_artifacts.py` |
| `v9rmvd` | GET_INTERACTIVE_HTML | Fetch quiz/flashcard HTML content | `_artifacts.py` |
| `fejl7e` | REMOVE_RECENTLY_VIEWED | Remove notebook from recent list | `_notebooks.py` |
| `ZwVcOc` | GET_USER_SETTINGS | Get user settings including output language | `_settings.py` |
| `hT54vc` | SET_USER_SETTINGS | Set user settings (e.g., output language) | `_settings.py` |

### Content Type Codes (StudioContentType)

| Code | Type | Used By |
|------|------|---------|
| 1 | Audio | Audio Overview |
| 2 | Report | Briefing Doc, Study Guide, Blog Post |
| 3 | Video | Video Overview |
| 4 | Quiz/Flashcards | Quiz (variant=2), Flashcards (variant=1) |
| 5 | Mind Map | Mind Map |
| 7 | Infographic | Infographic |
| 8 | Slide Deck | Slide Deck |
| 9 | Data Table | Data Table |

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
        "button:has-text('Create new')",
        "mat-card[role='button']:has-text('Create new notebook')",
    ],
    "notebook_card": [
        "mat-card:has(button:has-text('more_vert'))",
        "mat-card[role='button']:has(h3)",
    ],
    "notebook_menu": [
        "button[aria-label*='More options']",
    ],
}
```

### RPC: LIST_NOTEBOOKS (wXbhsf)

**Source:** `_notebooks.py::list()`

```python
params = [
    None,   # 0
    1,      # 1: Fixed value
    None,   # 2
    [2],    # 3: Fixed flag
]
```

### RPC: CREATE_NOTEBOOK (CCqFvf)

**Source:** `_notebooks.py::create()`

```python
params = [
    title,  # 0: Notebook title
    None,   # 1
    None,   # 2
    [2],    # 3: Fixed flag
    [1],    # 4: Fixed flag
]
```

### RPC: DELETE_NOTEBOOK (WWINqb)

**Source:** `_notebooks.py::delete()`

```python
params = [
    [notebook_id],  # 0: Single-nested notebook ID
    [2],            # 1: Fixed flag
]
```

### RPC: GET_NOTEBOOK (rLM1Ne)

**Source:** `_notebooks.py::get()`

```python
params = [
    notebook_id,  # 0
    None,         # 1
    [2],          # 2: Fixed flag
    None,         # 3
    0,            # 4: Fixed value
]
```

### RPC: REMOVE_RECENTLY_VIEWED (fejl7e)

**Source:** `_notebooks.py::remove_from_recent()`

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
        "button:has-text('+ Add sources')",
        "button:has-text('Add sources')",
    ],
    "source_card": ".single-source-container",
    "source_menu": "button[aria-label*='More options']",
    "remove_source": "button:has-text('Remove source')",
    "rename_source": "button:has-text('Rename source')",
}

ADD_SOURCE_MODAL = {
    "modal": "[role='dialog']",
    "website_tab": "button:has-text('Website')",
    "url_input": "textarea[placeholder*='links']",
    "copied_text_tab": "button:has-text('Copied text')",
    "submit_button": "button:has-text('Insert')",
}
```

### RPC: ADD_SOURCE (izAoDd) - URL

**Source:** `_sources.py::_add_url_source()`

```python
# URL goes at position [2] in an 8-element array
params = [
    [[None, None, [url], None, None, None, None, None]],  # 0: Source config
    notebook_id,                                           # 1: Notebook ID
    [2],                                                   # 2: Source type flag
    None,                                                  # 3
    None,                                                  # 4
]
```

### RPC: ADD_SOURCE (izAoDd) - Text

**Source:** `_sources.py::add_text()`

```python
# [title, content] at position [1] in an 8-element array
params = [
    [[None, [title, content], None, None, None, None, None, None]],  # 0
    notebook_id,                                                      # 1
    [2],                                                              # 2
    None,                                                             # 3
    None,                                                             # 4
]
```

### RPC: ADD_SOURCE (izAoDd) - YouTube

**Source:** `_sources.py::_add_youtube_source()`

```python
# YouTube URL at position [7] in an 11-element array (different from regular URL!)
params = [
    [[None, None, None, None, None, None, None, [url], None, None, 1]],  # 0
    notebook_id,                                                          # 1
    [2],                                                                  # 2
    [1, None, None, None, None, None, None, None, None, None, [1]],      # 3: Extra config
]
```

### RPC: DELETE_SOURCE (tGMBJ)

**Source:** `_sources.py::delete()`

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

**Source:** `_sources.py::rename()`

```python
# Different structure: None at [0], source_id at [1], title triple-nested at [2]
params = [
    None,               # 0
    [source_id],        # 1: Single-nested source ID
    [[[new_title]]],    # 2: Triple-nested title
]
```

### RPC: GET_SOURCE_GUIDE (tr032e)

**Source:** `_sources.py::get_guide()`

```python
# Quadruple-nested source ID!
params = [[[[source_id]]]]
```

---

## Chat Panel

### UI Selectors

```python
CHAT_SELECTORS = {
    "message_input": [
        "textarea[placeholder='Start typing...']",
        "textarea[aria-label='Query box']",
    ],
    "send_button": "button[aria-label='Submit']",
    "configure_button": "button[aria-label='Configure notebook']",
    "chat_history": "[role='log']",
    "message_bubble": [
        ".to-user-container",      # AI messages
        ".from-user-container",    # User messages
    ],
}

CHAT_CONFIG = {
    "modal": "configure-notebook-settings",
    "goal_default": "button[aria-label='Default button']",
    "goal_learning_guide": "button[aria-label*='Learning Guide']",
    "goal_custom": "button[aria-label='Custom button']",
    "length_shorter": "button[aria-label*='Shorter']",
    "length_longer": "button[aria-label*='Longer']",
    "save_button": "button:has-text('Save')",
}
```

### Query Endpoint (Streaming)

Chat queries use a **separate streaming endpoint**, not batchexecute:

```
POST /_/LabsTailwindUi/data/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GenerateFreeFormStreamed
```

### RPC: RENAME_NOTEBOOK (s0tc2d) - Rename Only

**Source:** `_notebooks.py::rename()`

```python
# Just rename, no chat config
params = [
    notebook_id,                                    # 0
    [[None, None, None, [None, new_title]]],        # 1: Nested title at [[[3][1]]]
]
```

### RPC: RENAME_NOTEBOOK (s0tc2d) - Configure Chat

**Source:** `_chat.py::configure()`

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
goal_array = [goal_value]                    # e.g., [1] for DEFAULT
# For CUSTOM: goal_array = [2, custom_prompt]

chat_settings = [goal_array, [response_length_value]]

params = [
    notebook_id,                                              # 0
    [[None, None, None, None, None, None, None, chat_settings]],  # 1: Settings at [[[7]]]
]
```

### RPC: GET_CONVERSATION_HISTORY (hPTbtc)

**Source:** `_chat.py::get_history()`

```python
params = [
    [],           # 0: Empty sources array
    None,         # 1
    notebook_id,  # 2
    limit,        # 3: Max conversations (e.g., 20)
]
```

---

## Studio Panel - Artifact Generation

### UI Selectors

```python
STUDIO_SELECTORS = {
    "artifact_button": ".create-artifact-button-container",
    "customize_icon": ".option-icon",  # Click THIS for customization!
    "add_note": "button:has-text('Add note')",
    "artifact_list": ".artifact-library-container",
    "artifact_row": ".artifact-item-button",
    "artifact_menu": ".artifact-more-button",
}

ARTIFACT_MENU = {
    "rename": "button:has-text('Rename')",
    "download": "button:has-text('Download')",
    "delete": "button:has-text('Delete')",
}
```

### Critical: Edit Icon vs Full Button

```python
# ✅ Click edit icon for customization dialog
await page.locator(".create-artifact-button-container:has-text('Audio') .option-icon").click()

# ❌ Clicking full button starts generation with defaults (skips customization!)
await page.locator(".create-artifact-button-container:has-text('Audio')").click()
```

### RPC: CREATE_VIDEO / Unified Artifact (R7cb6c)

**All artifact types use `R7cb6c` with different content type codes and nested configs.**

**Source:** `_artifacts.py`

#### Audio Overview (Type 1)

**Source:** `_artifacts.py::generate_audio()`

```python
source_ids_triple = [[[sid]] for sid in source_ids]  # [[[s1]], [[s2]], ...]
source_ids_double = [[sid] for sid in source_ids]    # [[s1], [s2], ...]

params = [
    [2],                              # 0: Fixed
    notebook_id,                      # 1
    [
        None,                         # [0]
        None,                         # [1]
        1,                            # [2]: StudioContentType.AUDIO
        source_ids_triple,            # [3]
        None,                         # [4]
        None,                         # [5]
        [
            None,
            [
                instructions,         # Focus/instructions text
                length_code,          # 1=SHORT, 2=DEFAULT, 3=LONG
                None,
                source_ids_double,
                language,             # "en"
                None,
                format_code,          # 1=DEEP_DIVE, 2=BRIEF, 3=CRITIQUE, 4=DEBATE
            ],
        ],                            # [6]
    ],                                # 2: Source config
]
```

#### Video Overview (Type 3)

**Source:** `_artifacts.py::generate_video()`

```python
params = [
    [2],
    notebook_id,
    [
        None,                         # [0]
        None,                         # [1]
        3,                            # [2]: StudioContentType.VIDEO
        source_ids_triple,            # [3]
        None,                         # [4]
        None,                         # [5]
        None,                         # [6]
        None,                         # [7]
        [
            None,
            None,
            [
                source_ids_double,
                language,             # "en"
                instructions,
                None,
                format_code,          # 1=EXPLAINER, 2=BRIEF
                style_code,           # 1=AUTO, 2=CUSTOM, 3=CLASSIC, 4=WHITEBOARD, etc.
            ],
        ],                            # [8]
    ],
]
```

#### Report (Type 2)

**Source:** `_artifacts.py::generate_report()`

```python
params = [
    [2],
    notebook_id,
    [
        None,                         # [0]
        None,                         # [1]
        2,                            # [2]: StudioContentType.REPORT
        source_ids_triple,            # [3]
        None,                         # [4]
        None,                         # [5]
        None,                         # [6]
        [
            None,
            [
                title,                # "Briefing Doc" / "Study Guide" / etc.
                description,          # Short description
                None,
                source_ids_double,
                language,             # "en"
                prompt,               # Detailed generation prompt
                None,
                True,
            ],
        ],                            # [7]
    ],
]
```

#### Quiz (Type 4, Variant 2)

**Source:** `_artifacts.py::generate_quiz()`

```python
params = [
    [2],
    notebook_id,
    [
        None,                         # [0]
        None,                         # [1]
        4,                            # [2]: StudioContentType.QUIZ_FLASHCARD
        source_ids_triple,            # [3]
        None,                         # [4]
        None,                         # [5]
        None,                         # [6]
        None,                         # [7]
        None,                         # [8]
        [
            None,
            [
                2,                    # Variant: 2=quiz, 1=flashcards
                None,
                instructions,
                None,
                None,
                None,
                None,
                [quantity_code, difficulty_code],  # quantity: 1=FEWER, 2=STANDARD
            ],                                     # difficulty: 1=EASY, 2=MEDIUM, 3=HARD
        ],                            # [9]
    ],
]
```

#### Flashcards (Type 4, Variant 1)

**Source:** `_artifacts.py::generate_flashcards()`

```python
params = [
    [2],
    notebook_id,
    [
        None,                         # [0]
        None,                         # [1]
        4,                            # [2]: StudioContentType.QUIZ_FLASHCARD
        source_ids_triple,            # [3]
        None,                         # [4]
        None,                         # [5]
        None,                         # [6]
        None,                         # [7]
        None,                         # [8]
        [
            None,
            [
                1,                    # Variant: 1=flashcards (vs 2=quiz)
                None,
                instructions,
                None,
                None,
                None,
                [difficulty_code, quantity_code],  # Note: reversed order from quiz!
            ],
        ],                            # [9]
    ],
]
```

#### Infographic (Type 7)

**Source:** `_artifacts.py::generate_infographic()`

```python
# Orientation: 1=LANDSCAPE, 2=PORTRAIT, 3=SQUARE
# Detail: 1=CONCISE, 2=STANDARD, 3=DETAILED

params = [
    [2],
    notebook_id,
    [
        None,                         # [0]
        None,                         # [1]
        7,                            # [2]: StudioContentType.INFOGRAPHIC
        source_ids_triple,            # [3]
        None, None, None, None, None, None, None, None, None, None,  # [4-13]
        [
            None,
            [instructions, language, None, orientation_code, detail_code],
        ],                            # [14]
    ],
]
```

#### Slide Deck (Type 8)

**Source:** `_artifacts.py::generate_slide_deck()`

```python
# Format: 1=DETAILED_DECK, 2=PRESENTER_SLIDES
# Length: 1=DEFAULT, 2=SHORT

params = [
    [2],
    notebook_id,
    [
        None,                         # [0]
        None,                         # [1]
        8,                            # [2]: StudioContentType.SLIDE_DECK
        source_ids_triple,            # [3]
        None, None, None, None, None, None, None, None, None, None, None, None,  # [4-15]
        [[instructions, language, format_code, length_code]],  # [16]
    ],
]
```

#### Data Table (Type 9)

**Source:** `_artifacts.py::generate_data_table()`

```python
params = [
    [2],
    notebook_id,
    [
        None,                         # [0]
        None,                         # [1]
        9,                            # [2]: StudioContentType.DATA_TABLE
        source_ids_triple,            # [3]
        None, None, None, None, None, None, None, None, None, None, None, None, None, None,  # [4-17]
        [None, [instructions, language]],  # [18]
    ],
]
```

#### Mind Map (Type 5) - Uses ACT_ON_SOURCES (yyryJe)

**Source:** `_artifacts.py::generate_mind_map()`

**Note:** Mind map uses a different RPC method than other artifacts.

```python
# RPC: ACT_ON_SOURCES (yyryJe), NOT CREATE_VIDEO
params = [
    source_ids_nested,                            # 0: [[[sid]] for sid in source_ids]
    None,                                         # 1
    None,                                         # 2
    None,                                         # 3
    None,                                         # 4
    ["interactive_mindmap", [["[CONTEXT]", ""]], ""],  # 5: Mind map command
    None,                                         # 6
    [2, None, [1]],                               # 7: Fixed config
]
```

### RPC: LIST_ARTIFACTS / POLL_STUDIO (gArtLc)

**Source:** `_artifacts.py::list()`

```python
params = [
    [2],
    notebook_id,
    'NOT artifact.status = "ARTIFACT_STATUS_SUGGESTED"',  # Filter string
]

# Response contains artifacts array with status:
# status = 1 → Processing
# status = 2 → Pending
# status = 3 → Completed
```

**Python API Note:** `artifacts.list()` also fetches mind maps from GET_NOTES_AND_MIND_MAPS and includes them as Artifact objects (type=5). This provides a unified list of all AI-generated content. Mind maps with status=2 (deleted) are filtered out.

---

## Notes

### RPC: CREATE_NOTE (CYK0Xb)

**Source:** `_notes.py::create()`

**Note:** Google ignores title/content in CREATE_NOTE. Must call UPDATE_NOTE after to set actual content.

```python
# Creates note with fixed placeholder values
params = [
    notebook_id,   # 0
    "",            # 1: Empty string (ignored)
    [1],           # 2: Fixed flag
    None,          # 3
    "New Note",    # 4: Placeholder title (ignored)
]
# Then call UPDATE_NOTE to set real title/content
```

### RPC: UPDATE_NOTE (cYAfTb)

**Source:** `_notes.py::update()`

```python
params = [
    notebook_id,                       # 0
    note_id,                           # 1
    [[[content, title, [], 0]]],       # 2: Triple-nested [content, title, [], 0]
]
```

### RPC: DELETE_NOTE (AH0mwd)

**Source:** `_notes.py::delete()`

**Important:** This is a **soft delete** - it clears note content but does NOT remove the note from the list. The note remains with `None` content and a status flag of `2`.

```python
params = [
    notebook_id,   # 0
    None,          # 1
    [note_id],     # 2: Single-nested note ID
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
```

---

## Note/Mind Map Data Structures

Notes and mind maps share the same storage system and are distinguished by content format.

### Active Note Structure

```python
[
    "note_id",           # Position 0: Note ID
    [
        "note_id",       # [1][0]: ID (duplicate)
        "content",       # [1][1]: Note content text
        [                # [1][2]: Metadata
            1,           # Type flag
            "user_id",   # User ID
            [ts, ns]     # [timestamp_seconds, nanoseconds]
        ],
        None,            # [1][3]: Unknown
        "title"          # [1][4]: Note title
    ]
]
```

### Active Mind Map Structure

```python
[
    "mind_map_id",       # Position 0: Mind map ID
    [
        "mind_map_id",   # [1][0]: ID (duplicate)
        '{"name": "Root", "children": [...]}',  # [1][1]: JSON with children/nodes
        [metadata],      # [1][2]: Same as notes
        None,            # [1][3]: Unknown
        "Mind Map Title" # [1][4]: Title
    ]
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

**Source:** `_notebooks.py::get_summary()`, `_notebooks.py::get_description()`

Gets AI-generated summary and suggested topics for a notebook.

```python
params = [
    notebook_id,  # 0: Notebook ID
    [2],          # 1: Fixed flag
]

# Called with source_path:
await rpc_call(
    RPCMethod.SUMMARIZE,
    params,
    source_path=f"/notebook/{notebook_id}",
)

# Response structure:
# [
#     [summary_text],           # [0][0]: Summary string
#     [[                        # [1][0]: Suggested topics array
#         [question, prompt],   # Each topic has question and prompt
#         ...
#     ]],
# ]
```

### RPC: GET_SHARE_STATUS (JFMDGd)

**Source:** Not yet implemented

Get the current share settings for a notebook.

```python
params = [
    notebook_id,  # 0: Notebook ID
    [2],          # 1: Fixed flag
]

# Response: Share status information
```

### RPC: SHARE_NOTEBOOK (QDyure)

**Source:** `_notebooks.py::share()`

Set notebook visibility (restricted vs anyone with link).

```python
# visibility: [0] = restricted, [1] = anyone with link
# access: [0, ""] = chat only?, [1, ""] = full notebook?

params = [
    [
        [
            notebook_id,  # Notebook ID
            None,
            visibility,   # [0]=restricted, [1]=public link
            access,       # [0, ""] or [1, ""] - access level
        ]
    ],
    1,            # Fixed value
    None,
    [2],          # Fixed flag
]
```

### RPC: SET_SHARE_ACCESS (via RENAME_NOTEBOOK s0tc2d)

**Source:** Not yet implemented (same RPC ID as RENAME_NOTEBOOK)

Set viewer access level (chat only vs full notebook).

**Note:** This uses the same RPC ID as RENAME_NOTEBOOK (`s0tc2d`) but with different parameter structure.

```python
# The access toggle is at index [8] in a deeply nested array
params = [
    notebook_id,  # 0: Notebook ID
    [
        [
            None, None, None, None,   # indices 0-3
            None, None, None, None,   # indices 4-7
            [[access_level]],         # index 8: [[1]] for chat only?
        ]
    ],
]
```

### Notebook Sharing Overview

**Sharing is a notebook-level setting.** When you share a notebook, ALL artifacts become accessible.

Notebooks have **two independent sharing toggles**:

1. **Visibility** (SHARE_NOTEBOOK - QDyure or SHARE_ARTIFACT - RGP97b):
   - `[0]` = Restricted (only specific people)
   - `[1]` = Anyone with the link

2. **Access Level** (SET_SHARE_ACCESS - s0tc2d):
   - `[[1]]` = Chat only (viewers can only use chat)
   - Other = Full notebook access

**Share URLs:**
- Notebook: `https://notebooklm.google.com/notebook/{notebook_id}`
- Artifact deep-link: `https://notebooklm.google.com/notebook/{notebook_id}?artifactId={artifact_id}`

The `?artifactId=xxx` parameter creates a deep link that opens the notebook and navigates to that specific artifact. Mind Maps cannot be shared (no public URLs).

---

## Source Refresh Operations

### RPC: REFRESH_SOURCE (FLmJqe)

**Source:** `_sources.py::refresh()`

Refresh a source to get updated content (for URL/Drive sources).

```python
params = [
    None,           # 0
    [source_id],    # 1: Single-nested source ID
    [2],            # 2: Fixed flag
]

# Called with source_path:
await rpc_call(
    RPCMethod.REFRESH_SOURCE,
    params,
    source_path=f"/notebook/{notebook_id}",
)
```

### RPC: CHECK_SOURCE_FRESHNESS (yR9Yof)

**Source:** `_sources.py::check_freshness()`

Check if a source needs to be refreshed.

```python
params = [
    None,           # 0
    [source_id],    # 1: Single-nested source ID
    [2],            # 2: Fixed flag
]

# Called with source_path:
await rpc_call(
    RPCMethod.CHECK_SOURCE_FRESHNESS,
    params,
    source_path=f"/notebook/{notebook_id}",
)

# Response: True = fresh, False = stale (needs refresh)
```

---

## Research Operations

Research allows searching the web or Google Drive for sources to add to notebooks.

### Source Type Codes

| Code | Source |
|------|--------|
| 1 | Web |
| 2 | Google Drive |

### RPC: START_FAST_RESEARCH (Ljjv0c)

**Source:** `_research.py::start()` with `mode="fast"`

Start a fast research session.

```python
# source_type: 1=Web, 2=Drive
params = [
    [query, source_type],  # 0: Query and source type
    None,                   # 1
    1,                      # 2: Fixed value
    notebook_id,            # 3: Notebook ID
]

# Called with source_path:
await rpc_call(
    RPCMethod.START_FAST_RESEARCH,
    params,
    source_path=f"/notebook/{notebook_id}",
)

# Response: [task_id, report_id, ...]
```

### RPC: START_DEEP_RESEARCH (QA9ei)

**Source:** `_research.py::start()` with `mode="deep"`

Start a deep research session (web only, more thorough).

```python
# Deep research only supports Web (source_type=1)
params = [
    None,                   # 0
    [1],                    # 1: Fixed flag
    [query, source_type],   # 2: Query and source type
    5,                      # 3: Fixed value
    notebook_id,            # 4: Notebook ID
]

# Called with source_path:
await rpc_call(
    RPCMethod.START_DEEP_RESEARCH,
    params,
    source_path=f"/notebook/{notebook_id}",
)

# Response: [task_id, report_id, ...]
```

### RPC: POLL_RESEARCH (e3bVqc)

**Source:** `_research.py::poll()`

Poll for research results.

```python
params = [
    None,          # 0
    None,          # 1
    notebook_id,   # 2: Notebook ID
]

# Called with source_path:
await rpc_call(
    RPCMethod.POLL_RESEARCH,
    params,
    source_path=f"/notebook/{notebook_id}",
)

# Response structure (per task):
# [
#     [task_id, [
#         ...,
#         query_info,           # [1]: [query_text, ...]
#         ...,
#         sources_and_summary,  # [3]: [[sources], summary_text]
#         status_code,          # [4]: 2=completed, other=in_progress
#     ]],
#     ...
# ]
```

### RPC: IMPORT_RESEARCH (LBwxtb)

**Source:** `_research.py::import_sources()`

Import selected research sources into the notebook.

```python
# Build source array from selected sources
source_array = []
for src in sources:
    source_data = [
        None,           # 0
        None,           # 1
        [url, title],   # 2: URL and title
        None,           # 3
        None,           # 4
        None,           # 5
        None,           # 6
        None,           # 7
        None,           # 8
        None,           # 9
        2,              # 10: Fixed value
    ]
    source_array.append(source_data)

params = [
    None,           # 0
    [1],            # 1: Fixed flag
    task_id,        # 2: Research task ID
    notebook_id,    # 3: Notebook ID
    source_array,   # 4: Array of sources to import
]

# Called with source_path:
await rpc_call(
    RPCMethod.IMPORT_RESEARCH,
    params,
    source_path=f"/notebook/{notebook_id}",
)

# Response: Imported sources with IDs
```

---

## User Settings

Global user settings that affect all notebooks in an account.

### RPC: GET_USER_SETTINGS (ZwVcOc)

**Source:** `_settings.py::get_output_language()`

Get user settings including the current output language.

```python
params = [
    None,                                                    # 0
    [1, None, None, None, None, None, None, None, None, None, [1]],  # 1: Fixed config
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
#     [6, 500, 300, 500000],        # [0][1]: Limits/quotas
#     [true, null, null, true, ["ja"]],  # [0][2]: Settings (language at [4][0])
#     [[1]],                         # [0][3]: Unknown
#     [true, 1, 3, 2]               # [0][4]: Feature flags
# ]]
#
# Language code at: result[0][2][4][0]
```

### RPC: SET_USER_SETTINGS (hT54vc)

**Source:** `_settings.py::set_output_language()`

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
- See `cli/language.py::SUPPORTED_LANGUAGES` for the full list of 80+ languages

---

## Artifact Management

### RPC: RENAME_ARTIFACT (rc3d8d)

**Source:** `_artifacts.py::rename()`

Rename an artifact.

```python
params = [
    [artifact_id, new_title],  # 0: Artifact ID and new title
    [["title"]],               # 1: Field mask (update title)
]

# Called with source_path:
await rpc_call(
    RPCMethod.RENAME_ARTIFACT,
    params,
    source_path=f"/notebook/{notebook_id}",
)
```

### RPC: EXPORT_ARTIFACT (Krh3pd)

**Source:** `_artifacts.py::export_report()`, `_artifacts.py::export_data_table()`, `_artifacts.py::export()`

Export an artifact to Google Docs or Sheets.

```python
# Export types:
# 1 = Google Docs
# 2 = Google Sheets

params = [
    None,          # 0
    artifact_id,   # 1: Artifact ID
    content,       # 2: Content to export (optional, can be None)
    title,         # 3: Title for exported document
    export_type,   # 4: 1=Docs, 2=Sheets
]

# Called with source_path:
await rpc_call(
    RPCMethod.EXPORT_ARTIFACT,
    params,
    source_path=f"/notebook/{notebook_id}",
)

# Response: Export result with document URL
```

### RPC: SHARE_ARTIFACT (RGP97b)

**Source:** `_notebooks.py::share()`

Toggle notebook sharing. **Sharing is a notebook-level setting** - when enabled, ALL artifacts in the notebook become accessible via their URLs.

Note: Mind Maps are NOT shareable (they don't have public URLs).

```python
# share_options: [1] for public, [0] for private
# artifact_id is optional - used to generate a deep-link URL to that specific artifact
params = [
    share_options,  # 0: [1] for public link, [0] for private
    notebook_id,    # 1: Notebook ID
    artifact_id,    # 2: Optional - artifact ID for deep-link URL
]

# Called with source_path:
await rpc_call(
    RPCMethod.SHARE_ARTIFACT,
    params,
    source_path=f"/notebook/{notebook_id}",
)

# Share URL format:
# - Notebook: https://notebooklm.google.com/notebook/{notebook_id}
# - Artifact deep-link: https://notebooklm.google.com/notebook/{notebook_id}?artifactId={artifact_id}
```

**Important:** The `?artifactId=xxx` URL is a **deep link** - it opens the shared notebook and navigates to that artifact. The artifact itself isn't independently shared.

### RPC: GET_INTERACTIVE_HTML (v9rmvd)

**Source:** `_artifacts.py::_get_artifact_content()`

Fetch HTML content for quiz or flashcard artifacts. Used for downloading these artifact types in various formats.

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
#     [html_content],         # index 9: HTML content array
#     ...
# ]]
#
# HTML content contains quiz questions or flashcard data
# that can be parsed into JSON, Markdown, or kept as HTML.
```

### RPC: GET_SUGGESTED_REPORTS (ciyUvf)

**Source:** `_artifacts.py::suggest_reports()` (currently uses ACT_ON_SOURCES alternative)

Get AI-suggested report formats based on notebook content.

```python
params = [
    [2],            # 0: Fixed flag (same pattern as LIST_ARTIFACTS)
    notebook_id,    # 1: Notebook ID
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

**Note:** The current implementation uses `ACT_ON_SOURCES` with `"suggested_report_formats"` command as an alternative approach. This dedicated RPC method provides the same functionality.

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
| `hizoJc` | GET_SOURCE | Broken | Code comments indicate this doesn't work; `get()` uses GET_NOTEBOOK instead |
| `qXyaNe` | DISCOVER_SOURCES | Unused | Purpose unknown, not called in implementation |
| `AHyHrd` | CREATE_AUDIO | Legacy | Superseded by unified CREATE_VIDEO (R7cb6c) |
| `VUsiyb` | GET_AUDIO | Legacy | Superseded by LIST_ARTIFACTS (gArtLc) |
| `sJDbic` | DELETE_AUDIO | Legacy | Superseded by DELETE_STUDIO (V5N4be) |
| `xpWGLf` | CREATE_ARTIFACT | Unused | Purpose unknown, CREATE_VIDEO handles all artifact types |
| `BnLyuf` | GET_ARTIFACT | Unused | Purpose unknown, LIST_ARTIFACTS provides artifact data |
| `LfTXoe` | LIST_ARTIFACTS_ALT | Unused | Alternative list method, main LIST_ARTIFACTS (gArtLc) is used |

**Why keep these?** These IDs are preserved in the codebase in case:
1. Google re-enables or changes their functionality
2. Future reverse-engineering reveals their purpose
3. They become useful for specific edge cases

**Note:** The unified `CREATE_VIDEO` (R7cb6c) method handles all artifact generation (audio, video, reports, quizzes, etc.) despite its name. The legacy audio-specific methods were likely from an earlier API version.
