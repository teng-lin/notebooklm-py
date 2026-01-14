# Flashcard & Quiz Download Investigation Findings

## Summary

**Successfully discovered how to download quiz and flashcard content from NotebookLM.**

The key discovery is a new RPC method `v9rmvd` (not previously documented) that returns the full quiz/flashcard content as a self-contained HTML document with embedded JSON data.

## Key Findings

### 1. New RPC Method: `v9rmvd`

- **RPC ID**: `v9rmvd`
- **Parameters**: `[artifact_id]`
- **Purpose**: Fetch quiz/flashcard content for display
- **Returns**: Full artifact data with HTML at position `[9][0]`

### 2. Response Structure

```
[
  [0] artifact_id
  [1] title
  [2] type (4 = quiz/flashcard)
  [3] [[source_ids]]
  [4] status
  [5-8] null
  [9] [html_content, metadata]  <-- The actual content!
  [10] timestamps
  ...
]
```

Position `[9][0]` contains a complete HTML document with:
- Embedded CSS/fonts
- JavaScript for interactivity
- **JSON data with all questions/answers**

### 3. Quiz Data Structure

The HTML contains a `<script>` tag with quiz data in this format:

```json
{
  "question": "What is the core philosophy of the project?",
  "answerOptions": [
    {
      "text": "The model itself is the agent...",
      "rationale": "This aligns with the project's philosophy...",
      "isCorrect": true
    },
    {
      "text": "The number and variety of tools...",
      "rationale": "While tools are necessary...",
      "isCorrect": false
    }
  ],
  "hint": "Consider the stated ratio of importance..."
}
```

### 4. Flashcard Data Structure

```json
{
  "f": "What is the fundamental loop that every coding agent is based on?",
  "b": "A loop where the model calls tools until it's done, and the results are appended to the message history."
}
```

- `f` = front (question)
- `b` = back (answer)

## Implementation Plan

### Step 1: Add RPC Method
Add to `src/notebooklm/rpc/types.py`:
```python
GET_ARTIFACT_CONTENT = "v9rmvd"  # Fetch quiz/flashcard/interactive content
```

### Step 2: Create Content Fetcher
In `_artifacts.py`, add method to fetch content:
```python
async def _get_artifact_content(self, artifact_id: str) -> list | None:
    """Fetch full artifact content including HTML for interactive types."""
    return await self._core.rpc_call(
        RPCMethod.GET_ARTIFACT_CONTENT,
        [artifact_id],
        source_path=f"/notebook/{self._current_notebook_id}",
        allow_null=True,
    )
```

### Step 3: Parse Quiz/Flashcard Data
Extract JSON from HTML response:
```python
import re
import json

def _parse_quiz_from_html(html: str) -> list[dict]:
    """Extract quiz questions from embedded HTML."""
    # Find the JSON data embedded in the HTML
    match = re.search(r'"questions"\s*:\s*\[(.*?)\]\s*}', html, re.DOTALL)
    if match:
        return json.loads(f"[{match.group(1)}]")
    return []

def _parse_flashcards_from_html(html: str) -> list[dict]:
    """Extract flashcard data from embedded HTML."""
    # Similar pattern for flashcards
    match = re.search(r'"cards"\s*:\s*\[(.*?)\]\s*}', html, re.DOTALL)
    if match:
        return json.loads(f"[{match.group(1)}]")
    return []
```

### Step 4: Download Methods
```python
async def download_quiz(
    self,
    notebook_id: str,
    output_path: Path,
    artifact_id: str | None = None,
    format: str = "json"  # json, markdown, html
) -> Path:
    """Download quiz questions."""

async def download_flashcards(
    self,
    notebook_id: str,
    output_path: Path,
    artifact_id: str | None = None,
    format: str = "json"  # json, markdown, html
) -> Path:
    """Download flashcard deck."""
```

### Step 5: Export Formats

**JSON Export:**
```json
{
  "title": "Agent Quiz",
  "questions": [
    {
      "question": "...",
      "options": [{"text": "...", "correct": true}, ...],
      "hint": "..."
    }
  ]
}
```

**Markdown Export:**
```markdown
# Agent Quiz

## Question 1
What is the core philosophy of the project?

- [ ] The agent's performance is primarily determined by clever engineering
- [x] The model itself is the agent, and the surrounding code's main job is to provide tools

**Hint:** Consider the stated ratio of importance...
```

**HTML Export:**
Return the original HTML for interactive use.

## Test Data

- Notebook: `167481cd-23a3-4331-9a45-c8948900bf91` (Claude Code High School)
- Quiz ID: `a0e4dca6-3bb0-4ed0-aea1-dfa91cf89767` (Agent Quiz)
- Flashcard ID: `173255d8-12b3-4c67-b925-a76ce6c71735` (Agent Flashcards)

## Files Created During Investigation

- `investigate_v9rmvd_direct.py` - Working script to fetch quiz/flashcard content
- `investigation_output/quiz_content_v9rmvd.json` - Full quiz response
- `investigation_output/flashcard_content_v9rmvd.json` - Full flashcard response

## Next Steps

1. Add `GET_ARTIFACT_CONTENT = "v9rmvd"` to rpc/types.py
2. Implement `download_quiz()` and `download_flashcards()` in `_artifacts.py`
3. Add CLI commands for downloading
4. Write unit tests with mock data
5. Test with real API (e2e tests)
