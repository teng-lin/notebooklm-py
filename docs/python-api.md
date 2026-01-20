# Python API Reference

**Status:** Active
**Last Updated:** 2026-01-20

Complete reference for the `notebooklm` Python library.

## Quick Start

```python
import asyncio
from notebooklm import NotebookLMClient

async def main():
    # Create client from saved authentication
    async with await NotebookLMClient.from_storage() as client:
        # List notebooks
        notebooks = await client.notebooks.list()
        print(f"Found {len(notebooks)} notebooks")

        # Create a new notebook
        nb = await client.notebooks.create("My Research")
        print(f"Created: {nb.id}")

        # Add sources
        await client.sources.add_url(nb.id, "https://example.com/article")

        # Ask a question
        result = await client.chat.ask(nb.id, "Summarize the main points")
        print(result.answer)

        # Generate a podcast
        status = await client.artifacts.generate_audio(nb.id)
        final = await client.artifacts.wait_for_completion(nb.id, status.task_id)
        print(f"Audio ready: {final.url}")

asyncio.run(main())
```

---

## Core Concepts

### Async Context Manager

The client must be used as an async context manager to properly manage HTTP connections:

```python
# Correct - uses context manager
async with await NotebookLMClient.from_storage() as client:
    ...

# Also correct - manual management
client = await NotebookLMClient.from_storage()
await client.__aenter__()
try:
    ...
finally:
    await client.__aexit__(None, None, None)
```

### Authentication

The client requires valid Google session cookies obtained via browser login:

```python
# From storage file (recommended)
client = await NotebookLMClient.from_storage()
client = await NotebookLMClient.from_storage("/path/to/storage_state.json")

# From AuthTokens directly
from notebooklm import AuthTokens
auth = AuthTokens(
    cookies={"SID": "...", "HSID": "...", ...},
    csrf_token="...",
    session_id="..."
)
client = NotebookLMClient(auth)
```

**Environment Variable Support:**

The library respects these environment variables for authentication:

| Variable | Description |
|----------|-------------|
| `NOTEBOOKLM_HOME` | Base directory for config files (default: `~/.notebooklm`) |
| `NOTEBOOKLM_AUTH_JSON` | Inline auth JSON - no file needed (for CI/CD) |

**Precedence** (highest to lowest):
1. Explicit `path` argument to `from_storage()`
2. `NOTEBOOKLM_AUTH_JSON` environment variable
3. `$NOTEBOOKLM_HOME/storage_state.json`
4. `~/.notebooklm/storage_state.json`

**CI/CD Example:**
```python
import os

# Set auth JSON from environment (e.g., GitHub Actions secret)
os.environ["NOTEBOOKLM_AUTH_JSON"] = '{"cookies": [...]}'

# Client automatically uses the env var
async with await NotebookLMClient.from_storage() as client:
    notebooks = await client.notebooks.list()
```

### Error Handling

The library raises `RPCError` for API failures:

```python
from notebooklm import RPCError

try:
    result = await client.notebooks.create("Test")
except RPCError as e:
    print(f"RPC failed: {e}")
    # Common causes:
    # - Session expired (re-run `notebooklm login`)
    # - Rate limited (wait and retry)
    # - Invalid parameters
```

### Authentication & Token Refresh

**Automatic Refresh:** The client automatically refreshes CSRF tokens when authentication errors are detected. This happens transparently during any API call - you don't need to handle it manually.

When an RPC call fails with an auth error (HTTP 401/403 or auth-related message):
1. The client fetches fresh tokens from the NotebookLM homepage
2. Waits briefly to avoid rate limiting
3. Retries the failed request automatically

**Manual Refresh:** For proactive refresh (e.g., before a long-running operation):

```python
async with await NotebookLMClient.from_storage() as client:
    # Manually refresh CSRF token and session ID
    await client.refresh_auth()
```

**Note:** If your session cookies have fully expired (not just CSRF tokens), you'll need to re-run `notebooklm login`.

---

## API Reference

### NotebookLMClient

Main client class providing access to all APIs.

```python
class NotebookLMClient:
    notebooks: NotebooksAPI    # Notebook operations
    sources: SourcesAPI        # Source management
    artifacts: ArtifactsAPI    # AI-generated content
    chat: ChatAPI              # Conversations
    research: ResearchAPI      # Web/Drive research
    notes: NotesAPI            # User notes

    @classmethod
    async def from_storage(cls, path: str = None) -> "NotebookLMClient"

    async def refresh_auth(self) -> AuthTokens
```

---

### NotebooksAPI (`client.notebooks`)

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `list()` | - | `list[Notebook]` | List all notebooks |
| `create(title)` | `title: str` | `Notebook` | Create a notebook |
| `get(notebook_id)` | `notebook_id: str` | `Notebook` | Get notebook details |
| `delete(notebook_id)` | `notebook_id: str` | `bool` | Delete a notebook |
| `rename(notebook_id, new_title)` | `notebook_id: str, new_title: str` | `Notebook` | Rename a notebook |
| `get_description(notebook_id)` | `notebook_id: str` | `NotebookDescription` | Get AI summary and topics |
| `get_summary(notebook_id)` | `notebook_id: str` | `str` | Get raw summary text |
| `share(notebook_id, settings=None)` | `notebook_id: str, settings: dict` | `Any` | Share notebook with settings |
| `remove_from_recent(notebook_id)` | `notebook_id: str` | `None` | Remove from recently viewed |
| `get_raw(notebook_id)` | `notebook_id: str` | `Any` | Get raw API response data |

**Example:**
```python
# List all notebooks
notebooks = await client.notebooks.list()
for nb in notebooks:
    print(f"{nb.id}: {nb.title} ({nb.sources_count} sources)")

# Create and rename
nb = await client.notebooks.create("Draft")
nb = await client.notebooks.rename(nb.id, "Final Version")

# Get AI-generated description (parsed with suggested topics)
desc = await client.notebooks.get_description(nb.id)
print(desc.summary)
for topic in desc.suggested_topics:
    print(f"  - {topic.question}")

# Get raw summary text (unparsed)
summary = await client.notebooks.get_summary(nb.id)
print(summary)

# Share a notebook
await client.notebooks.share(nb.id, settings={"public": True})
```

**get_summary vs get_description:**
- `get_summary()` returns the raw summary text string
- `get_description()` returns a `NotebookDescription` object with the parsed summary and a list of `SuggestedTopic` objects for suggested questions

---

### SourcesAPI (`client.sources`)

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `list(notebook_id)` | `notebook_id: str` | `list[Source]` | List sources |
| `get(notebook_id, source_id)` | `str, str` | `Source` | Get source details |
| `get_fulltext(notebook_id, source_id)` | `str, str` | `SourceFulltext` | Get full indexed text content |
| `get_guide(notebook_id, source_id)` | `str, str` | `dict` | Get AI-generated summary and keywords |
| `add_url(notebook_id, url)` | `str, str` | `Source` | Add URL source |
| `add_youtube(notebook_id, url)` | `str, str` | `Source` | Add YouTube video |
| `add_text(notebook_id, title, content)` | `str, str, str` | `Source` | Add text content |
| `add_file(notebook_id, path, mime_type=None)` | `str, Path, str` | `Source` | Upload file |
| `add_drive(notebook_id, file_id, title, mime_type)` | `str, str, str, str` | `Source` | Add Google Drive doc |
| `rename(notebook_id, source_id, new_title)` | `str, str, str` | `Source` | Rename source |
| `refresh(notebook_id, source_id)` | `str, str` | `bool` | Refresh URL/Drive source |
| `delete(notebook_id, source_id)` | `str, str` | `bool` | Delete source |

**Example:**
```python
# Add various source types
await client.sources.add_url(nb_id, "https://example.com/article")
await client.sources.add_youtube(nb_id, "https://youtube.com/watch?v=...")
await client.sources.add_text(nb_id, "My Notes", "Content here...")
await client.sources.add_file(nb_id, Path("./document.pdf"))

# List and manage
sources = await client.sources.list(nb_id)
for src in sources:
    print(f"{src.id}: {src.title} ({src.kind})")

await client.sources.rename(nb_id, src.id, "Better Title")
await client.sources.refresh(nb_id, src.id)  # Re-fetch URL content

# Get full indexed content (what NotebookLM uses for answers)
fulltext = await client.sources.get_fulltext(nb_id, src.id)
print(f"Content ({fulltext.char_count} chars): {fulltext.content[:500]}...")

# Get AI-generated summary and keywords
guide = await client.sources.get_guide(nb_id, src.id)
print(f"Summary: {guide['summary']}")
print(f"Keywords: {guide['keywords']}")
```

---

### ArtifactsAPI (`client.artifacts`)

#### Core Methods

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `list(notebook_id, type=None)` | `str, int` | `list[Artifact]` | List artifacts |
| `get(notebook_id, artifact_id)` | `str, str` | `Artifact` | Get artifact details |
| `delete(notebook_id, artifact_id)` | `str, str` | `bool` | Delete artifact |
| `rename(notebook_id, artifact_id, new_title)` | `str, str, str` | `None` | Rename artifact |
| `poll_status(notebook_id, task_id)` | `str, str` | `GenerationStatus` | Check generation status |
| `wait_for_completion(notebook_id, task_id, ...)` | `str, str, ...` | `GenerationStatus` | Wait for generation |

#### Type-Specific List Methods

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `list_audio(notebook_id)` | `str` | `list[Artifact]` | List audio overview artifacts |
| `list_video(notebook_id)` | `str` | `list[Artifact]` | List video overview artifacts |
| `list_reports(notebook_id)` | `str` | `list[Artifact]` | List report artifacts (Briefing Doc, Study Guide, Blog Post) |
| `list_quizzes(notebook_id)` | `str` | `list[Artifact]` | List quiz artifacts |
| `list_flashcards(notebook_id)` | `str` | `list[Artifact]` | List flashcard artifacts |
| `list_infographics(notebook_id)` | `str` | `list[Artifact]` | List infographic artifacts |
| `list_slide_decks(notebook_id)` | `str` | `list[Artifact]` | List slide deck artifacts |
| `list_data_tables(notebook_id)` | `str` | `list[Artifact]` | List data table artifacts |

#### Generation Methods

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `generate_audio(...)` | See below | `GenerationStatus` | Generate podcast |
| `generate_video(...)` | See below | `GenerationStatus` | Generate video |
| `generate_report(...)` | See below | `GenerationStatus` | Generate report |
| `generate_quiz(...)` | See below | `GenerationStatus` | Generate quiz |
| `generate_flashcards(...)` | See below | `GenerationStatus` | Generate flashcards |
| `generate_slide_deck(...)` | See below | `GenerationStatus` | Generate slide deck |
| `generate_infographic(...)` | See below | `GenerationStatus` | Generate infographic |
| `generate_data_table(...)` | See below | `GenerationStatus` | Generate data table |
| `generate_mind_map(...)` | See below | `dict` | Generate mind map |

#### Downloading Artifacts

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `download_audio(notebook_id, output_path, artifact_id=None)` | `str, str, str` | `str` | Download audio to file (MP4/MP3) |
| `download_video(notebook_id, output_path, artifact_id=None)` | `str, str, str` | `str` | Download video to file (MP4) |
| `download_infographic(notebook_id, output_path, artifact_id=None)` | `str, str, str` | `str` | Download infographic to file (PNG) |
| `download_slide_deck(notebook_id, output_path, artifact_id=None)` | `str, str, str` | `str` | Download slide deck as PDF |
| `download_report(notebook_id, output_path, artifact_id=None)` | `str, str, str` | `str` | Download report as Markdown (.md) |
| `download_mind_map(notebook_id, output_path, artifact_id=None)` | `str, str, str` | `str` | Download mind map as JSON (.json) |
| `download_data_table(notebook_id, output_path, artifact_id=None)` | `str, str, str` | `str` | Download data table as CSV (.csv) |
| `download_quiz(notebook_id, output_path, artifact_id=None, output_format="json")` | `str, str, str, str` | `str` | Download quiz (json/markdown/html) |
| `download_flashcards(notebook_id, output_path, artifact_id=None, output_format="json")` | `str, str, str, str` | `str` | Download flashcards (json/markdown/html) |

**Download Methods:**

```python
# Download the most recent completed audio overview
path = await client.artifacts.download_audio(nb_id, "podcast.mp4")

# Download a specific audio artifact by ID
path = await client.artifacts.download_audio(nb_id, "podcast.mp4", artifact_id="abc123")

# Download video overview
path = await client.artifacts.download_video(nb_id, "video.mp4")

# Download infographic
path = await client.artifacts.download_infographic(nb_id, "infographic.png")

# Download slide deck as PDF
path = await client.artifacts.download_slide_deck(nb_id, "./slides.pdf")
# Returns: "./slides.pdf"

# Download report as Markdown
path = await client.artifacts.download_report(nb_id, "./study-guide.md")
# Extracts markdown content from Briefing Doc, Study Guide, Blog Post, etc.

# Download mind map as JSON
path = await client.artifacts.download_mind_map(nb_id, "./concept-map.json")
# JSON structure: {"name": "Topic", "children": [{"name": "Subtopic", ...}]}

# Download data table as CSV
path = await client.artifacts.download_data_table(nb_id, "./data.csv")
# CSV uses UTF-8 with BOM encoding for Excel compatibility

# Download quiz as JSON (default)
path = await client.artifacts.download_quiz(nb_id, "quiz.json")

# Download quiz as markdown with answers marked
path = await client.artifacts.download_quiz(nb_id, "quiz.md", output_format="markdown")

# Download flashcards as JSON (normalizes f/b to front/back)
path = await client.artifacts.download_flashcards(nb_id, "cards.json")

# Download flashcards as markdown
path = await client.artifacts.download_flashcards(nb_id, "cards.md", output_format="markdown")
```

**Notes:**
- If `artifact_id` is not specified, downloads the first completed artifact of that type
- Raises `ValueError` if no completed artifact is found
- Some URLs require browser-based download (handled automatically)
- Report downloads extract the markdown content from the artifact
- Mind map downloads return a JSON tree structure with `name` and `children` fields
- Data table downloads parse the complex rich-text format into CSV rows/columns
- Quiz/flashcard formats: `json` (structured), `markdown` (readable), `html` (raw)

#### Export Methods

Export artifacts to Google Docs or Google Sheets.

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `export_report(notebook_id, artifact_id, title, export_type)` | `str, str, str, ExportType` | `Any` | Export report to Google Docs/Sheets |
| `export_data_table(notebook_id, artifact_id, title)` | `str, str, str` | `Any` | Export data table to Google Sheets |
| `export(notebook_id, artifact_id, content, title, export_type)` | `str, str, str, str, ExportType` | `Any` | Generic export to Docs/Sheets |

**Export Types (ExportType enum):**
- `ExportType.DOCS` (1): Export to Google Docs
- `ExportType.SHEETS` (2): Export to Google Sheets

```python
from notebooklm import ExportType

# Export a report to Google Docs
result = await client.artifacts.export_report(
    nb_id,
    artifact_id="report_123",
    title="My Briefing Doc",
    export_type=ExportType.DOCS
)
# result contains the Google Docs URL

# Export a data table to Google Sheets
result = await client.artifacts.export_data_table(
    nb_id,
    artifact_id="table_456",
    title="Research Data"
)
# result contains the Google Sheets URL

# Generic export (e.g., export any artifact to Sheets)
result = await client.artifacts.export(
    nb_id,
    artifact_id="artifact_789",
    title="Exported Content",
    export_type=ExportType.SHEETS
)
```

**Generation Methods:**

```python
# Audio (podcast)
status = await client.artifacts.generate_audio(
    notebook_id,
    source_ids=None,           # List of source IDs (None = all)
    instructions="...",        # Custom instructions
    audio_format=AudioFormat.DEEP_DIVE,  # DEEP_DIVE, BRIEF, CRITIQUE, DEBATE
    audio_length=AudioLength.DEFAULT,    # SHORT, DEFAULT, LONG
    language="en"
)

# Video
status = await client.artifacts.generate_video(
    notebook_id,
    source_ids=None,
    instructions="...",
    video_format=VideoFormat.EXPLAINER,  # EXPLAINER, BRIEF
    video_style=VideoStyle.AUTO_SELECT,  # AUTO_SELECT, CLASSIC, WHITEBOARD, KAWAII, ANIME, etc.
    language="en"
)

# Report
status = await client.artifacts.generate_report(
    notebook_id,
    source_ids=None,
    title="...",
    description="...",
    format=ReportFormat.STUDY_GUIDE,  # BRIEFING_DOC, STUDY_GUIDE, BLOG_POST, CUSTOM
    language="en"
)

# Quiz
status = await client.artifacts.generate_quiz(
    notebook_id,
    source_ids=None,
    instructions="...",
    quantity=QuizQuantity.STANDARD,    # FEWER, STANDARD
    difficulty=QuizDifficulty.MEDIUM,  # EASY, MEDIUM, HARD
    language="en"
)
```

**Waiting for Completion:**

```python
# Start generation
status = await client.artifacts.generate_audio(nb_id)

# Wait with polling
final = await client.artifacts.wait_for_completion(
    nb_id,
    status.task_id,
    timeout=300,      # Max wait time in seconds
    poll_interval=5   # Seconds between polls
)

if final.is_complete:
    print(f"Download URL: {final.url}")
else:
    print(f"Failed or timed out: {final.status}")
```

---

### ChatAPI (`client.chat`)

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `ask(notebook_id, question, ...)` | `str, str, ...` | `AskResult` | Ask a question |
| `configure(notebook_id, ...)` | `str, ...` | `bool` | Set chat persona |
| `get_history(notebook_id)` | `str` | `list[ConversationTurn]` | Get conversation |

**ask() Parameters:**
```python
async def ask(
    notebook_id: str,
    question: str,
    source_ids: list[str] | None = None,  # Limit to specific sources (None = all)
    conversation_id: str | None = None,   # Continue existing conversation
) -> AskResult
```

**Example:**
```python
# Ask questions (uses all sources)
result = await client.chat.ask(nb_id, "What are the main themes?")
print(result.answer)

# Access source references (cited in answer as [1], [2], etc.)
for ref in result.references:
    print(f"Citation {ref.citation_number}: Source {ref.source_id}")

# Ask using only specific sources
result = await client.chat.ask(
    nb_id,
    "Summarize the key points",
    source_ids=["src_001", "src_002"]
)

# Continue conversation
result = await client.chat.ask(
    nb_id,
    "Can you elaborate on the first point?",
    conversation_id=result.conversation_id
)

# Configure persona
await client.chat.configure(
    nb_id,
    goal=ChatGoal.LEARNING_GUIDE,
    response_length=ChatResponseLength.LONGER,
    custom_prompt="Focus on practical applications"
)
```

---

### ResearchAPI (`client.research`)

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `start(notebook_id, query, source, mode)` | `str, str, str="web", str="fast"` | `dict` | Start research (mode: "fast" or "deep") |
| `poll(notebook_id)` | `str` | `dict` | Check research status |
| `import_sources(notebook_id, task_id, sources)` | `str, str, list` | `list[dict]` | Import findings |

**Method Signatures:**

```python
async def start(
    notebook_id: str,
    query: str,
    source: str = "web",   # "web" or "drive"
    mode: str = "fast",    # "fast" or "deep" (deep only for web)
) -> dict:
    """
    Returns: {"task_id": str, "report_id": str, "notebook_id": str, "query": str, "mode": str}
    Raises: ValueError if source/mode combination is invalid
    """

async def poll(notebook_id: str) -> dict:
    """
    Returns: {"task_id": str, "status": str, "query": str, "sources": list, "summary": str}
    Status is "completed", "in_progress", or "no_research"
    """

async def import_sources(notebook_id: str, task_id: str, sources: list[dict]) -> list[dict]:
    """
    sources: List of dicts with 'url' and 'title' keys
    Returns: List of imported sources with 'id' and 'title'
    """
```

**Example:**
```python
# Start fast web research (default)
result = await client.research.start(nb_id, "AI safety regulations")
task_id = result["task_id"]

# Start deep web research
result = await client.research.start(nb_id, "quantum computing", source="web", mode="deep")
task_id = result["task_id"]

# Start fast Drive research
result = await client.research.start(nb_id, "project docs", source="drive", mode="fast")

# Poll until complete
import asyncio
while True:
    status = await client.research.poll(nb_id)
    if status["status"] == "completed":
        break
    await asyncio.sleep(10)

# Import discovered sources
imported = await client.research.import_sources(nb_id, task_id, status["sources"][:5])
print(f"Imported {len(imported)} sources")
```

---

### NotesAPI (`client.notes`)

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `list(notebook_id)` | `str` | `list[Note]` | List text notes (excludes mind maps) |
| `create(notebook_id, title="New Note", content="")` | `str, str, str` | `Note` | Create note |
| `get(notebook_id, note_id)` | `str, str` | `Optional[Note]` | Get note by ID |
| `update(notebook_id, note_id, content, title)` | `str, str, str, str` | `None` | Update note content and title |
| `delete(notebook_id, note_id)` | `str, str` | `bool` | Delete note |
| `list_mind_maps(notebook_id)` | `str` | `list[Any]` | List mind maps in the notebook |
| `delete_mind_map(notebook_id, mind_map_id)` | `str, str` | `bool` | Delete a mind map |

**Example:**
```python
# Create and manage notes
note = await client.notes.create(nb_id, title="Meeting Notes", content="Discussion points...")
notes = await client.notes.list(nb_id)

# Update a note
await client.notes.update(nb_id, note.id, "Updated content", "New Title")

# Delete a note
await client.notes.delete(nb_id, note.id)
```

**Mind Maps:**

Mind maps are stored internally using the same structure as notes but contain JSON data with hierarchical node information. The `list()` method excludes mind maps automatically, while `list_mind_maps()` returns only mind maps.

```python
# List all mind maps in a notebook
mind_maps = await client.notes.list_mind_maps(nb_id)
for mm in mind_maps:
    mm_id = mm[0]  # Mind map ID is at index 0
    print(f"Mind map: {mm_id}")

# Delete a mind map
await client.notes.delete_mind_map(nb_id, mind_map_id)
```

**Note:** Mind maps are detected by checking if the content contains `'"children":' or `'"nodes":'` keys, which indicate JSON mind map data structure.

---

### SettingsAPI (`client.settings`)

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `get_output_language()` | none | `Optional[str]` | Get current output language setting |
| `set_output_language(language)` | `str` | `Optional[str]` | Set output language for artifact generation |

**Example:**
```python
# Get current language setting
lang = await client.settings.get_output_language()
print(f"Current language: {lang}")  # e.g., "en", "ja", "zh_Hans"

# Set language for artifact generation
result = await client.settings.set_output_language("ja")  # Japanese
print(f"Language set to: {result}")
```

**Important:** Language is a **GLOBAL setting** that affects all notebooks in your account. Supported languages include:
- `en` (English), `ja` (日本語), `zh_Hans` (中文简体), `zh_Hant` (中文繁體)
- `ko` (한국어), `es` (Español), `fr` (Français), `de` (Deutsch), `pt_BR` (Português)
- And [80+ other languages](cli-reference.md#language-commands)

---

## Data Types

### Notebook

```python
@dataclass
class Notebook:
    id: str
    title: str
    created_at: Optional[datetime]
    sources_count: int
    is_owner: bool
```

### Source

```python
@dataclass
class Source:
    id: str
    title: Optional[str]
    url: Optional[str]
    created_at: Optional[datetime]

    @property
    def kind(self) -> SourceType:
        """Get source type as SourceType enum."""
```

**Type Identification:**

Use the `.kind` property to identify source types. It returns a `SourceType` enum which is also a `str`, enabling both enum and string comparisons:

```python
from notebooklm import SourceType

# Enum comparison (recommended)
if source.kind == SourceType.PDF:
    print("This is a PDF")

# String comparison (also works)
if source.kind == "pdf":
    print("This is a PDF")

# Use in f-strings
print(f"Type: {source.kind}")  # "Type: pdf"
```

### Artifact

```python
@dataclass
class Artifact:
    id: str
    title: str
    status: int                     # 1=processing, 2=pending, 3=completed
    created_at: Optional[datetime]
    url: Optional[str]

    @property
    def kind(self) -> ArtifactType:
        """Get artifact type as ArtifactType enum."""

    @property
    def is_completed(self) -> bool:
        """Check if artifact generation is complete."""

    @property
    def is_quiz(self) -> bool:
        """Check if this is a quiz artifact."""

    @property
    def is_flashcards(self) -> bool:
        """Check if this is a flashcards artifact."""
```

**Type Identification:**

Use the `.kind` property to identify artifact types. It returns an `ArtifactType` enum which is also a `str`:

```python
from notebooklm import ArtifactType

# Enum comparison (recommended)
if artifact.kind == ArtifactType.AUDIO:
    print("This is an audio overview")

# String comparison (also works)
if artifact.kind == "audio":
    print("This is an audio overview")

# Check specific types
if artifact.is_quiz:
    print("This is a quiz")
elif artifact.is_flashcards:
    print("This is a flashcard deck")
```

### AskResult

```python
@dataclass
class AskResult:
    answer: str                        # The answer text with inline citations [1], [2], etc.
    conversation_id: str               # ID for follow-up questions
    turn_number: int                   # Turn number in conversation
    is_follow_up: bool                 # Whether this was a follow-up question
    references: list[ChatReference]    # Source references cited in the answer
    raw_response: str                  # First 1000 chars of raw API response

@dataclass
class ChatReference:
    source_id: str                     # UUID of the source
    citation_number: int | None        # Citation number in answer (1, 2, etc.)
    cited_text: str | None             # Actual text passage being cited
    start_char: int | None             # Start position in source content
    end_char: int | None               # End position in source content
    chunk_id: str | None               # Internal chunk ID (for debugging)
```

**Important:** The `cited_text` field often contains only a snippet or section header, not the full quoted passage. The `start_char`/`end_char` positions reference NotebookLM's internal chunked index, which does not directly correspond to positions in the raw fulltext returned by `get_fulltext()`.

Use `SourceFulltext.find_citation_context()` to locate citations in the fulltext:

```python
fulltext = await client.sources.get_fulltext(notebook_id, ref.source_id)
matches = fulltext.find_citation_context(ref.cited_text)  # Returns list[(context, position)]

if matches:
    context, pos = matches[0]  # First match
    if len(matches) > 1:
        print(f"Warning: {len(matches)} matches found, using first")
else:
    context = None  # Not found - may occur if source was modified
```

**Tip:** Cache `fulltext` when processing multiple citations from the same source to avoid repeated API calls.

### SourceFulltext

```python
@dataclass
class SourceFulltext:
    source_id: str                     # UUID of the source
    title: str                         # Source title
    content: str                       # Full indexed text content
    url: str | None                    # Original URL (if applicable)
    char_count: int                    # Character count

    @property
    def kind(self) -> SourceType:
        """Get source type as SourceType enum."""

    def find_citation_context(
        self,
        cited_text: str,
        context_chars: int = 200,
    ) -> list[tuple[str, int]]:
        """Search for citation text, return list of (context, position) tuples."""
```

**Type Identification:**

Like `Source`, use the `.kind` property to get the source type:

```python
fulltext = await client.sources.get_fulltext(nb_id, source_id)
print(f"Content type: {fulltext.kind}")  # "pdf", "web_page", etc.
```

---

## Enums

### Audio Generation

```python
class AudioFormat(Enum):
    DEEP_DIVE = 1   # In-depth discussion
    BRIEF = 2       # Quick summary
    CRITIQUE = 3    # Critical analysis
    DEBATE = 4      # Two-sided debate

class AudioLength(Enum):
    SHORT = 1
    DEFAULT = 2
    LONG = 3
```

### Video Generation

```python
class VideoFormat(Enum):
    EXPLAINER = 1
    BRIEF = 2

class VideoStyle(Enum):
    AUTO_SELECT = 1
    CUSTOM = 2
    CLASSIC = 3
    WHITEBOARD = 4
    KAWAII = 5
    ANIME = 6
    WATERCOLOR = 7
    RETRO_PRINT = 8
    HERITAGE = 9
    PAPER_CRAFT = 10
```

### Quiz/Flashcards

```python
class QuizQuantity(Enum):
    FEWER = 1
    STANDARD = 2

class QuizDifficulty(Enum):
    EASY = 1
    MEDIUM = 2
    HARD = 3
```

### Reports

```python
class ReportFormat(Enum):
    BRIEFING_DOC = 1
    STUDY_GUIDE = 2
    BLOG_POST = 3
    CUSTOM = 4
```

### Infographics

```python
class InfographicOrientation(Enum):
    LANDSCAPE = 1
    PORTRAIT = 2
    SQUARE = 3

class InfographicDetail(Enum):
    CONCISE = 1
    STANDARD = 2
    DETAILED = 3
```

### Slide Decks

```python
class SlideDeckFormat(Enum):
    DETAILED_DECK = 1
    PRESENTER_SLIDES = 2

class SlideDeckLength(Enum):
    DEFAULT = 1
    SHORT = 2
```

### Export

```python
class ExportType(Enum):
    DOCS = 1    # Export to Google Docs
    SHEETS = 2  # Export to Google Sheets
```

### Source and Artifact Types

```python
class SourceType(str, Enum):
    """Source types - use with source.kind property.

    This is a str enum, enabling both enum and string comparisons:
        source.kind == SourceType.PDF   # True
        source.kind == "pdf"            # Also True
    """
    GOOGLE_DOCS = "google_docs"
    GOOGLE_SLIDES = "google_slides"
    GOOGLE_SPREADSHEET = "google_spreadsheet"
    PDF = "pdf"
    PASTED_TEXT = "pasted_text"
    WEB_PAGE = "web_page"
    YOUTUBE = "youtube"
    MARKDOWN = "markdown"
    DOCX = "docx"
    CSV = "csv"
    IMAGE = "image"
    MEDIA = "media"
    UNKNOWN = "unknown"

class ArtifactType(str, Enum):
    """Artifact types - use with artifact.kind property.

    This is a str enum that hides internal variant complexity.
    Quizzes and flashcards are distinguished automatically.
    """
    AUDIO = "audio"
    VIDEO = "video"
    REPORT = "report"
    QUIZ = "quiz"
    FLASHCARDS = "flashcards"
    MIND_MAP = "mind_map"
    INFOGRAPHIC = "infographic"
    SLIDES = "slides"
    DATA_TABLE = "data_table"
    UNKNOWN = "unknown"

class SourceStatus(Enum):
    PROCESSING = 1  # Source is being processed (indexing content)
    READY = 2       # Source is ready for use
    ERROR = 3       # Source processing failed
    PREPARING = 5   # Source is being prepared/uploaded (pre-processing stage)
```

**Usage Example:**
```python
from notebooklm import SourceType, ArtifactType

# List sources by type using .kind property
sources = await client.sources.list(nb_id)
for src in sources:
    if src.kind == SourceType.PDF:
        print(f"PDF: {src.title}")
    elif src.kind == SourceType.MEDIA:
        print(f"Audio/Video: {src.title}")
    elif src.kind == SourceType.IMAGE:
        print(f"Image (OCR'd): {src.title}")
    elif src.kind == SourceType.UNKNOWN:
        print(f"Unknown type: {src.title}")

# List artifacts by type using .kind property
artifacts = await client.artifacts.list(nb_id)
for art in artifacts:
    if art.kind == ArtifactType.AUDIO:
        print(f"Audio: {art.title}")
    elif art.kind == ArtifactType.VIDEO:
        print(f"Video: {art.title}")
    elif art.kind == ArtifactType.QUIZ:
        print(f"Quiz: {art.title}")
```

### Chat Configuration

```python
class ChatGoal(Enum):
    DEFAULT = 1        # General purpose
    CUSTOM = 2         # Uses custom_prompt
    LEARNING_GUIDE = 3 # Educational focus

class ChatResponseLength(Enum):
    DEFAULT = 1
    LONGER = 4
    SHORTER = 5

class ChatMode(Enum):
    """Predefined chat modes for common use cases (service-level enum)."""
    DEFAULT = "default"          # General purpose
    LEARNING_GUIDE = "learning_guide"  # Educational focus
    CONCISE = "concise"          # Brief responses
    DETAILED = "detailed"        # Verbose responses
```

**ChatGoal vs ChatMode:**
- `ChatGoal` is an RPC-level enum used with `client.chat.configure()` for low-level API configuration
- `ChatMode` is a service-level enum providing predefined configurations for common use cases

---

## Advanced Usage

### Custom RPC Calls

For undocumented features, you can make raw RPC calls:

```python
from notebooklm.rpc import RPCMethod

async with await NotebookLMClient.from_storage() as client:
    # Access the core client for raw RPC
    result = await client._core.rpc_call(
        RPCMethod.SOME_METHOD,
        params=[...],
        source_path="/notebook/123"
    )
```

### Handling Rate Limits

Google rate limits aggressive API usage:

```python
import asyncio
from notebooklm import RPCError

async def safe_create_notebooks(client, titles):
    for title in titles:
        try:
            await client.notebooks.create(title)
        except RPCError:
            # Wait and retry on rate limit
            await asyncio.sleep(10)
            await client.notebooks.create(title)
        # Add delay between operations
        await asyncio.sleep(2)
```

### Streaming Chat Responses

The chat endpoint supports streaming (internal implementation):

```python
# Standard (non-streaming) - recommended
result = await client.chat.ask(nb_id, "Question")
print(result.answer)

# Streaming is handled internally by the library
# The ask() method returns the complete response
```
