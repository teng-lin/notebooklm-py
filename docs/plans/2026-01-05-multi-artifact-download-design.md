# Multi-Artifact Download Design

**Date:** 2026-01-05
**Status:** Approved
**Primary Use Case:** LLM agents with occasional human users

## Overview

Enhance download commands to intelligently handle scenarios where multiple artifacts of the same type exist in a notebook. Support natural language selection (latest, earliest, by name) with smart defaults that work seamlessly for LLM-driven interactions.

## Command Interface

All download commands support natural language selection:

```bash
notebooklm download audio [OUTPUT_PATH] [OPTIONS]
notebooklm download video [OUTPUT_PATH] [OPTIONS]
notebooklm download slide-deck [OUTPUT_DIR] [OPTIONS]
notebooklm download infographic [OUTPUT_PATH] [OPTIONS]

Arguments:
  OUTPUT_PATH    Optional. File path or directory.
                 - If omitted: smart defaults (see below)
                 - If directory: saves there with artifact title
                 - If file path: uses exact name

Selection Options:
  -n, --notebook TEXT     Notebook ID (uses current if not set)
  --latest               Download most recent (default if multiple found)
  --earliest             Download oldest
  --all                  Download all (OUTPUT_PATH must be directory)
  --name TEXT            Match by artifact title (fuzzy search)
  --artifact-id ID       Exact artifact ID
```

## Smart Defaults

### Single Artifact Downloads
**Default OUTPUT_PATH:** `./[artifact-title].[ext]`

Downloads to current working directory with auto-named file.

Example: `./Deep Dive Overview.mp3`

### Multiple Artifacts (--all)
**Default OUTPUT_PATH:** `./<type>/`

Creates subdirectory by artifact type:
- Audio: `./audio/`
- Video: `./video/`
- Slide-deck: `./slide-deck/`
- Infographic: `./infographic/`

## Selection Behavior

### Single Artifact
- Ignores all selection flags
- Downloads with title as filename
- Output: `Downloaded: "Deep Dive Overview.mp3"`

### Multiple Artifacts (No Flags)
- Automatically selects latest by creation timestamp
- Output: `Downloaded latest: "Deep Dive Overview.mp3" (1 of 3 total)`
- Hint: `Tip: Use --earliest, --name, or --all for other artifacts`

### Multiple + --earliest
- Selects oldest by creation timestamp
- Output: `Downloaded earliest: "First Recording.mp3" (1 of 3 total)`

### Multiple + --name "debate"
- Fuzzy matches against artifact titles (case-insensitive, substring)
- If multiple matches: picks latest matching artifact
- If no matches: error with available options
- Output: `Downloaded: "Debate Format.mp3" (matched by name)`

### Multiple + --all
- OUTPUT_PATH must be directory
- Downloads all artifacts, auto-naming by title
- Handles title conflicts with " (2)", " (3)" suffixes
- Output: `Downloaded 3 audio overviews to ./audio/`

### Zero Artifacts
- Error: `No audio artifacts found. Generate one with: notebooklm generate audio`

## Usage Examples

### LLM-Friendly Commands
```bash
# LLM: "download the latest audio"
$ notebooklm download audio
→ ./Deep Dive Overview.mp3

# LLM: "download all the videos"
$ notebooklm download video --all
→ ./video/Explainer.mp4
→ ./video/Brief Summary.mp4

# LLM: "download the debate audio"
$ notebooklm download audio --name debate
→ ./Debate Format.mp3

# LLM: "download the earliest infographic to ~/images/"
$ notebooklm download infographic ~/images/ --earliest
→ ~/images/Timeline Overview.png
```

## Implementation

### Helper Functions

**Artifact Selection:**
```python
def select_artifact(
    artifacts: list,
    latest: bool = True,
    earliest: bool = False,
    name: Optional[str] = None,
    artifact_id: Optional[str] = None
) -> tuple[Any, str]:
    """
    Select an artifact from a list based on criteria.

    Returns: (selected_artifact, selection_reason)
    Raises: ValueError if no match or invalid criteria
    """
```

Responsibilities:
1. Validate mutually exclusive flags
2. Filter/sort artifacts based on criteria
3. Return selected artifact + human-readable reason
4. Raise clear errors with suggestions when no match

**Filename Sanitization:**
```python
def artifact_title_to_filename(
    title: str,
    extension: str,
    existing_files: set
) -> str:
    """
    Convert artifact title to safe filename.

    - Sanitizes special characters
    - Handles conflicts with " (2)", " (3)", etc.
    - Adds extension
    """
```

### Integration Points

1. Modify existing download commands (`download_audio`, `download_video`, etc.)
2. Make OUTPUT_PATH optional with Click
3. Add selection flags (--latest, --earliest, --all, --name)
4. Call `select_artifact()` before `client.download_*()`
5. Handle smart defaults for OUTPUT_PATH

## Error Handling

### Invalid Flag Combinations
```bash
$ notebooklm download audio --latest --earliest
Error: Cannot use --latest and --earliest together
```

### --all with File Path
```bash
$ notebooklm download audio ./podcast.mp3 --all
Error: --all requires OUTPUT_PATH to be a directory, not a file
```

### No Matches for --name
```bash
$ notebooklm download audio --name "xyz"
Error: No audio artifacts matching "xyz"
Available:
  - Deep Dive Overview (Jan 5, 15:30)
  - Brief Summary (Jan 4, 10:15)
  - Debate Format (Jan 3, 09:00)
```

### Filename Conflicts (--all)
- Append " (2)", " (3)" for duplicate titles
- Example: "Overview.mp3", "Overview (2).mp3"

### Download Failures (--all)
- Continue downloading others if one fails
- Print summary: `Downloaded 2 of 3 (1 failed)`

## Design Principles

1. **LLM-First:** Commands work without explicit paths or indices
2. **Smart Defaults:** Latest is implicit default for multiple artifacts
3. **Natural Language:** Use semantic selectors (--name, --latest) not numeric indices
4. **Non-Blocking:** Never prompt for interactive input
5. **Helpful Hints:** Print tips when multiple options exist
6. **YAGNI:** No complex filtering or sorting beyond latest/earliest/name

## Testing Considerations

- Test with 0, 1, and multiple artifacts
- Verify fuzzy name matching (case-insensitive, substring)
- Verify filename sanitization and conflict resolution
- Test all flag combinations for mutual exclusivity
- Verify smart default paths for each artifact type
- Test --all with directory creation
