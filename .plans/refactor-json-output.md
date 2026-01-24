# Plan: Refactor CLI --json-out Pattern

## Goal
1. Standardize all existing `--json` options to use `@json_option` decorator
2. Add `--json` support to ALL commands for consistency
3. Use `output_result` helper to reduce conditional boilerplate

## Current State
- 21 commands use inline `@click.option("--json", ...)` → standardize to `@json_option`
- 12 commands already use `@json_option` (generate.py, language.py) → no changes
- 15 commands have NO `--json` support → add it
- `download.py` uses dynamic registration with options in a list → special handling
- `output_result` helper already exists in `helpers.py:456-480`

---

## Step 1: Standardize existing `--json` to `@json_option`

### Pattern
Replace:
```python
@click.option("--json", "json_output", is_flag=True, help="...")
```
With:
```python
@json_option
```

### Files to Update

#### `source.py`
- **Import**: Add `from .options import json_option` after line 27
- **Lines**: 69, 140, 488, 553, 660
- **Commands**: `source list`, `source add`, `source fulltext`, `source guide`, `source wait`

#### `share.py`
- **Import**: Add `from .options import json_option` after line 18
- **Lines**: 80, 157, 206, 264, 326, 370
- **Commands**: `share status`, `share public`, `share view-level`, `share add`, `share update`, `share remove`

#### `artifact.py`
- **Import**: Add `from .options import json_option` after line 21
- **Lines**: 82, 337, 407
- **Commands**: `artifact list`, `artifact wait`, `artifact suggestions`

#### `research.py`
- **Import**: Add `from .options import json_option` after line 16
- **Lines**: 52, 120
- **Commands**: `research status`, `research wait`

#### `notebook.py`
- **Import**: Add `from .options import json_option` after line 25
- **Lines**: 32, 75
- **Commands**: `list`, `create`

#### `session.py`
- **Import**: Add `from .options import json_option` after existing imports
- **Lines**: 236, 360
- **Commands**: `status`, `auth check`

#### `chat.py`
- **Import**: Add `from .options import json_option` after line 20
- **Lines**: 50
- **Commands**: `ask`

**Total: 21 replacements across 7 files**

---

## Step 2: Handle `download.py` (Dynamic Registration)

`download.py` uses a list of options applied programmatically, not decorators.

### File: `download.py`
- **Import**: Add `from .options import json_option` after line 27
- **Line 98**: Replace inline option with decorator object

**Before (line 98):**
```python
click.option("--json", "json_output", is_flag=True, help="Output JSON instead of text"),
```

**After:**
```python
json_option,  # Decorator object works in option lists
```

**Note**: The group-level `--json` at line 151 (`group_json`) should remain as-is since it uses a different parameter name for UUID download mode.

---

## Step 3: Add `--json` to commands that lack it

### Commands to Add `--json`

#### `notebook.py` (3 commands)
| Command | Line | Add After |
|---------|------|-----------|
| `delete` | 107 | After `--yes` option |
| `rename` | 145 | After `--notebook` option |
| `summary` | 171 | After `--topics` option |

#### `source.py` (3 commands)
| Command | Line | Add After |
|---------|------|-----------|
| `delete` | 259 | After `--yes` option |
| `rename` | 293 | After `--notebook` option |
| `refresh` | 321 | After `--notebook` option |

#### `note.py` (6 commands) - NEW FILE
- **Import**: Add after line 22:
  ```python
  from .options import json_option
  from .helpers import json_output_response
  ```
- **Update helpers import** (line 17-22): Add `json_output_response`

| Command | Line | Add After |
|---------|------|-----------|
| `list` | 52 | After `@with_client` decorator |
| `create` | 94 | After `--title` option |
| `get` | 128 | After `@with_client` decorator |
| `save` | 162 | After `--content` option |
| `rename` | 193 | After `@with_client` decorator |
| `delete` | 226 | After `--yes` option |

### Implementation Pattern for New `--json`

For each command, add the decorator and update the function signature:

```python
# Before
@note.command("list")
@click.option("-n", "--notebook", ...)
@with_client
def note_list(ctx, notebook_id, client_auth):

# After
@note.command("list")
@click.option("-n", "--notebook", ...)
@json_option
@with_client
def note_list(ctx, notebook_id, json_output, client_auth):
```

Then add JSON output handling inside the `_run()` function:

```python
async def _run():
    async with NotebookLMClient(client_auth) as client:
        notes = await client.notes.list(nb_id)

        # Add JSON output
        if json_output:
            json_output_response({
                "notes": [
                    {"id": n.id, "title": n.title, "preview": n.content[:50] if n.content else ""}
                    for n in notes if isinstance(n, Note)
                ],
                "count": len(notes)
            })
            return

        # Existing text output...
```

**Total: 12 commands to add `--json`**

---

## Step 4 (Optional): Migrate to `output_result` helper

The `output_result` helper exists at `helpers.py:456-480`. Commands can be migrated incrementally.

### Pattern
**Before:**
```python
async def _run():
    async with NotebookLMClient(client_auth) as client:
        notebooks = await client.notebooks.list()

        if json_output:
            json_output_response({"notebooks": [...], "count": len(notebooks)})
            return

        table = Table(...)
        for nb in notebooks:
            table.add_row(...)
        console.print(table)
```

**After:**
```python
async def _run():
    async with NotebookLMClient(client_auth) as client:
        notebooks = await client.notebooks.list()

        # Prepare data for JSON
        data = {
            "notebooks": [
                {"id": nb.id, "title": nb.title, ...}
                for nb in notebooks
            ],
            "count": len(notebooks)
        }

        # Define render function (closure captures `notebooks`)
        def render():
            table = Table(...)
            for nb in notebooks:
                table.add_row(...)
            console.print(table)

        # Single call handles both modes
        output_result(json_output, data, render)
```

### Add Import
Update helpers import to include `output_result`:
```python
from .helpers import (
    console,
    json_output_response,
    output_result,  # Add this
    ...
)
```

### Suggested Files for Migration
Start with `notebook.py` as proof of concept:
- `list_cmd` (lines 31-71)
- `create_cmd` (lines 73-97)

---

## Verification

### After Each File (Step 1-3)
```bash
# Verify import works
python -c "from notebooklm.cli.[module] import [command]; print('OK')"

# Verify --json appears in help
notebooklm [command] --help | grep -i json
```

### Final Verification
```bash
# Format and lint
ruff format src/notebooklm/cli/ && ruff check src/notebooklm/cli/

# Type check
mypy src/notebooklm --ignore-missing-imports

# Run CLI tests
pytest tests/unit/cli/ -v

# Smoke test JSON output on multiple commands
notebooklm list --json
notebooklm source list --json
notebooklm note list --json
notebooklm download audio --help | grep json
```

---

## Summary

| Category | Files | Commands |
|----------|-------|----------|
| Standardize existing `--json` | 8 | 22 |
| Add new `--json` | 3 | 12 |
| Already using `@json_option` | 2 | 12 |
| **Total** | **10** | **46** |

---

## Files NOT Needing Changes

- `generate.py` - Already uses `@json_option` via `@generate_options`
- `language.py` - Already uses `@json_option` via `@standard_options`
- `error_handler.py` - Error handling, not command options
- `options.py` - Defines decorators, no changes needed
- `helpers.py` - `output_result` already exists
